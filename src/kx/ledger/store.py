"""The ledger: one append-only log per writer (user@machine) in <store>/log/, and a SQLite cache of them.

Every change is a line in this machine's log: a row inserted, updated or deleted, with when and by whom. Nobody but
its writer appends to a log, so logs copied between machines (kx sync) never conflict. The cache (cache.sqlite) is
the logs replayed in time order: queries read it; it is rebuilt when another writer's log changes, and dropped when
its schema changes. A few columns only this machine needs (run heartbeats, when games were last fetched) and the
`workers` table are kept in the cache only.

Runs, tasks, findings and submissions have ids made of a number and a name: r0012-fluent-sun, T4-quiet-otter. The
number is the next one this machine knows of, the name a random one: two machines writing at the same time may pick
the same number, not the same id. `r12` (or `12`, or `fluent-sun`) finds it when only one has that number here.

Each entry has a random key (`u`): an entry seen twice (a log restored and kept beside the shared one, a renamed
machine) is applied once. Two machines that did make the same id each have a full name for their row, the id and a
suffix from its writer (r0012-fluent-sun~3f2a); the log refers to such a row by its full name, so every machine finds
the same row whatever it shows it as.
"""

from __future__ import annotations

import getpass
import hashlib
import heapq
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import tarfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kx.ledger import names
from kx.ledger.config import Comp, KxError

SCHEMA = [
    """CREATE TABLE runs (
        id         TEXT PRIMARY KEY,              -- r0012-fluent-sun
        n          INTEGER NOT NULL,
        name       TEXT NOT NULL,
        writer     TEXT,                          -- the log it was made in: user@machine
        created    TEXT NOT NULL,
        finished   TEXT,
        status     TEXT NOT NULL,                 -- running | done | failed
        kind       TEXT NOT NULL DEFAULT 'experiment',  -- experiment | probe
        hypothesis TEXT,
        config     TEXT,
        command    TEXT,
        author     TEXT,
        task       TEXT,
        parent     TEXT,
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
        heartbeat  TEXT,                          -- this machine only
        env        TEXT,
        gpus       INTEGER,                       -- GPUs the run declared (kx exec --gpus): charged as gpu_hours
        metrics    TEXT,
        error      TEXT
    )""",
    "CREATE INDEX runs_n ON runs(n)",
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
        run_id      TEXT NOT NULL,
        protocol_id TEXT NOT NULL,
        value       REAL NOT NULL,
        se          REAL,
        n           INTEGER,
        breakdown   TEXT,
        created     TEXT NOT NULL,
        PRIMARY KEY (run_id, protocol_id)
    )""",
    "CREATE INDEX scores_protocol ON scores(protocol_id)",
    "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts TEXT NOT NULL, author TEXT, text TEXT NOT NULL)",
    "CREATE INDEX notes_run ON notes(run_id)",
    "CREATE TABLE tags (run_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (run_id, tag))",
    """CREATE TABLE findings (
        id            TEXT PRIMARY KEY,           -- F3-bold-comet
        n             INTEGER NOT NULL,
        name          TEXT NOT NULL,
        writer        TEXT,
        ts            TEXT NOT NULL,
        author        TEXT,
        text          TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'open',  -- open | withdrawn | superseded
        evidence      TEXT,                          -- JSON list of run ids
        task          TEXT,
        reviewed_by   TEXT,
        reviewed      TEXT,
        review_note   TEXT,
        superseded_by TEXT
    )""",
    "CREATE TABLE finding_tags (finding_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (finding_id, tag))",
    """CREATE TABLE tasks (
        id         TEXT PRIMARY KEY,              -- T4-quiet-otter
        n          INTEGER NOT NULL,
        name       TEXT NOT NULL,
        writer     TEXT,
        title      TEXT NOT NULL,
        body       TEXT,
        priority   INTEGER NOT NULL DEFAULT 0,
        depends    TEXT,                          -- JSON list of task ids
        status     TEXT NOT NULL DEFAULT 'open',  -- open | active | waiting | done | dropped
        owner      TEXT,
        waiting_on TEXT,                          -- what a waiting task waits for (run:r0012-..., T3-..., a command)
        result     TEXT,                          -- positive | negative | inconclusive
        outcome    TEXT,
        branch     TEXT,
        worktree   TEXT,
        created    TEXT NOT NULL,
        created_by TEXT,
        claimed    TEXT,                          -- when its owner took it: of two claims at once, the earlier keeps it
        updated    TEXT
    )""",
    "CREATE TABLE task_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, ts TEXT NOT NULL, author TEXT, text TEXT NOT NULL)",
    "CREATE INDEX task_notes_task ON task_notes(task)",
    """CREATE TABLE artifacts (
        sha       TEXT PRIMARY KEY,               -- sha256 of the file that gets uploaded
        run_id    TEXT NOT NULL,
        name      TEXT NOT NULL,
        path      TEXT NOT NULL,                  -- relative to the store: artifacts/<sha12>/<name>
        size      INTEGER,
        manifest  TEXT,
        smoke     TEXT NOT NULL DEFAULT 'unchecked',  -- unchecked | passed | failed
        smoke_log TEXT,
        created   TEXT NOT NULL,
        actor     TEXT
    )""",
    """CREATE TABLE submissions (
        id          TEXT PRIMARY KEY,             -- S5-brave-lynx
        n           INTEGER NOT NULL,
        name        TEXT NOT NULL,
        writer      TEXT,
        ref         TEXT,                         -- Kaggle's id, NULL until synced
        run_id      TEXT,
        task        TEXT,
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
        episodes_at TEXT,                         -- this machine only
        updated     TEXT
    )""",
    "CREATE INDEX submissions_ref ON submissions(ref)",
    """CREATE TABLE submission_episodes (
        submission_id TEXT NOT NULL,
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
    "CREATE TABLE submission_scores (submission_id TEXT NOT NULL, ts TEXT NOT NULL, public REAL, episodes INTEGER)",
    """CREATE TABLE pushes (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id  TEXT NOT NULL,
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
        writer TEXT,                              -- the log it was charged in: user@machine (budget scopes)
        metric TEXT NOT NULL,                     -- submissions | kaggle_gpu_hours | gpu_hours | tokens | cloud_usd
        amount REAL NOT NULL,
        ref    TEXT
    )""",
    "CREATE INDEX usage_metric ON usage(metric, ts)",
    """CREATE TABLE workers (                     -- this machine only
        id         TEXT PRIMARY KEY,              -- launch id
        agent      TEXT NOT NULL,
        harness    TEXT NOT NULL,
        model      TEXT,
        task       TEXT,
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
    "CREATE TABLE applied (log TEXT PRIMARY KEY, size INTEGER NOT NULL)",  # how much of each log is in here
    "CREATE TABLE entries (k TEXT PRIMARY KEY)",  # the entries in here: one seen twice is applied once
    # rows two machines gave the same id (writer: whose row; local: its id here; first: replayed first), and
    # submissions taken twice (writer '*': the id that was dropped, local: the one kept)
    """CREATE TABLE ids (id TEXT NOT NULL, writer TEXT NOT NULL, local TEXT NOT NULL, first INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (id, writer))""",
]
CACHE_VERSION = 3  # another version: the cache is dropped and rebuilt from the logs
LOGGED = ("runs", "protocols", "scores", "notes", "tags", "findings", "finding_tags", "tasks", "task_notes", "artifacts",
          "submissions", "submission_episodes", "submission_scores", "pushes", "lb_snapshots", "usage", "settings")
LOCAL_COLUMNS = {"runs": ("heartbeat",), "submissions": ("episodes_at",)}
LOCAL_SETTINGS = ("synced", "lb_bounds", "share", "clock", "last_entry", "lost_claims",
                  "current_task")  # current_task: {owner: the task they started last here}
# where the id of a row of each entity table appears (Store._rename): plain columns, and JSON lists of ids
REFS = {"runs": (("runs", "parent"), ("scores", "run_id"), ("notes", "run_id"), ("tags", "run_id"),
                 ("artifacts", "run_id"), ("submissions", "run_id"), ("pushes", "run_id")),
        "tasks": (("runs", "task"), ("findings", "task"), ("submissions", "task"), ("task_notes", "task")),
        "findings": (("finding_tags", "finding_id"), ("findings", "superseded_by")),
        "submissions": (("submission_episodes", "submission_id"), ("submission_scores", "submission_id"))}
JSON_REFS = {"runs": (("findings", "evidence"),), "tasks": (("tasks", "depends"),)}
ENTITIES = {"runs": "r", "tasks": "T", "findings": "F", "submissions": "S"}
WRITE_SQL = re.compile(r"^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+(\w+)",
                       re.IGNORECASE)

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


def author() -> str:
    """Who is recorded on runs and notes: the worker (KX_AGENT) or the user (machine.yaml `user:`, else the login)."""
    from kx import sandbox
    return os.environ.get("KX_AGENT") or os.environ.get("KX_AUTHOR") or str(sandbox.machine().get("user") or getpass.getuser())


def machine_name() -> str:
    """This machine in the logs: the part of writer() after the user."""
    return writer().split("@", 1)[1]


def owner(name: str | None = None) -> str:
    """Who holds a task: the actor and its machine (codex-1@laptop, me@laptop), so the same worker name from comp.yaml
    on two machines, or one login on two rented boxes, are two owners."""
    return f"{name or author()}@{machine_name()}"


def writer() -> str:
    """This machine's log: user@machine (~/.config/kx/machine.yaml `user:` and `name:`, else the login and the host
    name). The person and their agents here write to it; each line says which."""
    from kx import sandbox
    m = sandbox.machine()
    user = str(m.get("user") or getpass.getuser())
    host = str(m.get("name") or socket.gethostname().split(".")[0]).lower()
    return re.sub(r"[^A-Za-z0-9_.@+-]", "-", f"{user}@{host}")


def full_id(key: str, writer: str) -> str:
    """The full name of a row whose id another machine's row has too: the id and a suffix from its writer."""
    return f"{key}~{hashlib.sha1(writer.encode()).hexdigest()[:4]}"


def entity_key(t: str, n: int, name: str) -> str:
    return f"r{n:04d}-{name}" if t == "runs" else f"{ENTITIES[t]}{n}-{name}"


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


def read_log(path: Path, start: int = 0) -> tuple[list[tuple[str, dict]], int]:
    """The entries of a log from byte `start` as (key, entry), and where its last complete line ends (a line being
    written is not one yet). The key is the entry's `u`; a line from before entries had one is known by its writer,
    where it is in its log and its text (the same in a copy of the log)."""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    end = data.rfind(b"\n") + 1
    out, pos, base = [], start, path.stem.split("+", 1)[0]
    for line in data[:end].split(b"\n")[:-1] if end else []:
        at, pos = pos, pos + len(line) + 1
        if line.strip():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue  # a damaged line: skipped, never fatal
            if isinstance(e, dict):
                out.append((e.get("u") or f"{base}#{at}#{hashlib.sha1(line).hexdigest()[:12]}", e))
    return out, start + end


def complete(path: Path) -> int:
    """Where the last complete line of a log ends."""
    return path.read_bytes().rfind(b"\n") + 1 if path.exists() else 0


def mine(log: str, me: str) -> bool:
    """Logs this machine writes: its own and the histories it imported (user@machine+import-…)."""
    return log == me or log.startswith(me + "+")


def missing_ledger(comp: Comp) -> KxError:
    """No ledger (no log/, nor an old ledger.sqlite) where comp.yaml says, on a machine that shares nothing: kx does
    not start an empty one in its place (a moved store, an unmounted disk or a deleted .kx/ would go on as a project
    with no history); kx init does, for a new project or a new machine. A machine with a share goes on: kx sync
    brings the history back."""
    backups = sorted((comp.store / "backups").glob("log-*.tar.gz"), key=lambda p: p.stat().st_mtime) \
        if (comp.store / "backups").is_dir() else []
    fix = "a new project, or this project's first command on this machine: kx init (it creates the ledger; then " \
          "kx sync brings what other machines wrote). If the store moved or its disk is not mounted, point comp.yaml " \
          "store: at it"
    if backups:
        fix += f"; to restore the newest backup: tar xzf {backups[-1]} -C {comp.store}"
    return KxError(f"no ledger at {comp.log_dir}: kx starts a new one only with kx init", fix=fix)


class Store:
    def __init__(self, comp: Comp, *, auto_backup: bool = False, create: bool = False):
        self.comp = comp
        if not create and not comp.log_dir.is_dir() and not (comp.store / "ledger.sqlite").is_file():  # (kx import)
            from kx.ledger.sync import location
            if (share := location(comp)) is None:
                raise missing_ledger(comp)
            print(f"kx: no ledger at {comp.log_dir} yet: kx sync brings this project's history from {share} (what is "
                  "written here before it is kept beside it)", file=sys.stderr)
        for d in (comp.store, comp.runs_dir, comp.log_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.writer = writer()
        self.former = self._names()
        self.log_path = comp.log_dir / f"{self.writer}.jsonl"
        self._pending: list[tuple[Path, str]] = []
        self._cols: dict[str, set] = {}
        self._ids: dict = {"full": {}, "by": {}, "first": {}, "out": {}}
        self.db = sqlite3.connect(comp.db_path, timeout=60, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=60000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self._prepare()
        self._load_ids()
        if auto_backup:
            self.maybe_backup()

    def close(self):
        self.db.close()

    # -- who this store writes as ------------------------------------------------------------------------------------

    def _names(self) -> list[str]:
        """The names this store wrote under before this one (machine.yaml name: changed): their logs are still this
        machine's, uploaded by kx sync, and so are the tasks, runs and budgets recorded under them."""
        p = self.comp.store / "writers"
        names = p.read_text().split() if p.exists() else []
        if self.writer not in names:
            with open(p, "a") as f:
                f.write(self.writer + "\n")
        return [n for n in names if n != self.writer]

    def forget_name(self, name: str):
        """`name` is another machine's (it wrote under it too): not this store's any more."""
        p = self.comp.store / "writers"
        names = p.read_text().split() if p.exists() else []
        p.write_text("".join(n + "\n" for n in names if n != name))
        self.former = [n for n in self.former if n != name]

    @property
    def writers(self) -> list[str]:
        """This machine's name in the logs, and the ones it had before."""
        return [self.writer, *self.former]

    def mine(self, log: str) -> bool:
        """A log (or a row's writer) of this machine, under its name now or one it had."""
        return any(mine(log, w) for w in self.writers)

    def owners(self, name: str | None = None) -> tuple[str, ...]:
        """What a task's owner is when `name` (default: whoever runs this) holds it here: name@machine, under this
        machine's names now and before, and the bare name (an owner from before owners had machines)."""
        n = name or author()
        return (*(f"{n}@{w.split('@', 1)[1]}" for w in self.writers), n)

    def owns(self, holder: str | None, name: str | None = None) -> bool:
        return bool(holder) and holder in self.owners(name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- reading -------------------------------------------------------------------------------------------------

    def x(self, sql: str, params=()) -> sqlite3.Cursor:
        """Any SQL, for reading and for the tables only this machine has (workers). A write to a logged table here
        would never reach the log: refused."""
        m = WRITE_SQL.match(sql)
        if m and m.group(1).lower() in LOGGED:
            raise KxError(f"internal: {m.group(1)} is written through Store.insert/update/delete, not x()")
        return self.db.execute(sql, params)

    def one(self, sql: str, params=()):
        return self.x(sql, params).fetchone()

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.x(sql, params).fetchall()

    # -- writing: every change is a log line ----------------------------------------------------------------------

    @contextmanager
    def transaction(self):
        """BEGIN IMMEDIATE: concurrent writers here serialize. The changes go to the log just before COMMIT (a crash
        in between leaves lines the cache has not: the next open applies them)."""
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._pending = []
        wrote: list[tuple[Path, int]] = []
        try:
            self._load_ids()  # another process may have changed them
            self._catch_up_own()
            yield
            self._flush(wrote)
            self.db.execute("COMMIT")
        except BaseException:
            self._pending = []
            for path, size in wrote:  # the lines of a change that failed (disk full, the COMMIT) are not in the log
                with open(path, "r+b") as f:
                    f.truncate(size)
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self._load_ids()
            raise

    def insert(self, t: str, row: dict, *, on: str | None = None) -> int:
        """Insert a row (on: 'ignore' when it may exist, 'replace' to overwrite it). Returns 1, or 0 when ignored
        (nothing is logged then)."""
        return self._write({"t": t, "op": "ins", "row": row, **({"on": on} if on else {})})

    def update(self, t: str, values: dict, **where) -> int:
        """Set `values` on the rows matching `where` (column = value, None matching NULL). Only rows it changes count;
        an update that changes nothing is not logged."""
        if not where:
            raise KxError("internal: update without a where")
        local = {k: v for k, v in values.items() if k in LOCAL_COLUMNS.get(t, ())}
        logged = {k: v for k, v in values.items() if k not in local}
        cond, params = self._where(where)
        n = 0
        with self.transaction():
            if local:
                n = self.db.execute(f"UPDATE {t} SET {', '.join(f'{k}=?' for k in local)} WHERE {cond}",
                                    (*local.values(), *params)).rowcount
            if logged:
                same = " AND ".join(f"{k} IS ?" for k in logged)
                if self.db.execute(f"SELECT 1 FROM {t} WHERE {cond} AND NOT ({same}) LIMIT 1",
                                   (*params, *logged.values())).fetchone():
                    n = self._write({"t": t, "op": "upd", "set": logged, "where": where})
        return n

    def delete(self, t: str, **where) -> int:
        if not where:
            raise KxError("internal: delete without a where")
        return self._write({"t": t, "op": "del", "where": where})

    def new(self, t: str, row: dict) -> str:
        """A new run, task, finding or submission: the next number, a fresh name; returns its id."""
        with self.transaction():
            n = (self.db.execute(f"SELECT MAX(n) FROM {t}").fetchone()[0] or 0) + 1
            name = names.fresh(r[0] for r in self.db.execute(f"SELECT name FROM {t}"))
            key = entity_key(t, n, name)
            self.insert(t, {"id": key, "n": n, "name": name, "writer": self.writer, **row})
        return key

    def _write(self, e: dict) -> int:
        with self.transaction():
            n = self._apply(e)
            if n:
                line = {"c": self._tick(), "at": now(), "by": author(), "u": secrets.token_hex(8), **self._to_log(e)}
                self.db.execute("INSERT OR IGNORE INTO entries VALUES (?)", (line["u"],))
                self._pending.append((self.log_path, json.dumps(line, default=str)))
                self._advance((line["c"], line["at"], self.log_path.stem))
        return n

    def _last(self) -> tuple | None:
        """The replay key (logical time, wall time, log) of the latest entry in the cache."""
        row = self.db.execute("SELECT value FROM settings WHERE key='last_entry'").fetchone()
        return tuple(json.loads(row[0])) if row else None

    def _advance(self, key: tuple):
        last = self._last()
        if last is None or tuple(key) > last:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('last_entry', ?)", (json.dumps(list(key)),))

    def _tick(self) -> int:
        """The next logical time (Lamport's): past every entry this machine has seen, its own and the other machines',
        so a change made after seeing another machine's row replays after it, whatever the clocks say. The wall time
        (`at`) only orders changes made without seeing each other."""
        row = self.db.execute("SELECT value FROM settings WHERE key='clock'").fetchone()
        c = (int(row[0]) if row else 0) + 1
        self._seen(c)
        return c

    def _seen(self, c):
        if c:
            self.db.execute("INSERT INTO settings VALUES ('clock', ?) ON CONFLICT(key) DO UPDATE SET "
                            "value=MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))", (str(int(c)),))

    def log_to(self, path: Path, entries: list[dict]) -> int:
        """Apply entries (with their own at/by) and write them to another log of this machine (an import)."""
        n = 0
        with self.transaction():
            log = path.stem
            for e in entries:
                e = {**e, "u": e.get("u") or secrets.token_hex(8)}
                n += self._apply(e, log, e["u"]) > 0
                self._pending.append((path, json.dumps(e, default=str)))
                self._advance((e.get("c") or 0, e.get("at") or "", log))
        return n

    def _flush(self, wrote: list):
        by_path: dict[Path, list[str]] = {}
        for path, line in self._pending:
            by_path.setdefault(path, []).append(line)
        for path, lines in by_path.items():
            wrote.append((path, path.stat().st_size if path.exists() else 0))
            with open(path, "ab") as f:
                f.write("".join(line + "\n" for line in lines).encode())
                f.flush()
                os.fsync(f.fileno())
            self.db.execute("INSERT OR REPLACE INTO applied VALUES (?,?)", (path.stem, path.stat().st_size))
        self._pending = []

    @staticmethod
    def _where(where: dict) -> tuple[str, tuple]:
        return " AND ".join(f"{k} IS ?" for k in where), tuple(where.values())

    def _columns(self, t: str) -> set:
        if t not in self._cols:
            self._cols[t] = {c[1] for c in self.db.execute(f"PRAGMA table_info({t})")}
        return self._cols[t]

    def _apply(self, e: dict, log: str | None = None, key: str | None = None) -> int:
        """Run one entry on the cache. `log`: replaying that log (unknown tables and columns, from a newer kx, are
        skipped; a row both sides inserted is kept once; an entry already applied, `key`, is skipped). Live writes
        (log None) raise instead."""
        if key is not None and not self.db.execute("INSERT OR IGNORE INTO entries VALUES (?)", (key,)).rowcount:
            return 0
        t, op = e.get("t"), e.get("op")
        if t not in LOGGED:
            if log is None:
                raise KxError(f"internal: {t} is not a logged table")
            return 0
        cols = self._columns(t)
        if log is not None and self._ids["by"]:
            e = self._from_log(e, log.split("+", 1)[0])
        n = self._run(t, op, e, cols, log)
        if t == "submissions" and n:
            self._merge_taken()
        return n

    def _merge_taken(self):
        """A Kaggle submission two rows stand for: two machines' `kx sync` each took it as external before the other's
        log arrived, or one did before the log of the machine that made it. The one made (else the one replayed
        first) is the submission; the other's id leads to it."""
        for r in self.db.execute(
                "SELECT s.id, s.ref FROM submissions s WHERE s.kind='external' AND s.ref IS NOT NULL AND EXISTS "
                "(SELECT 1 FROM submissions o WHERE o.ref=s.ref AND o.id<>s.id AND "
                "(o.kind IS NOT 'external' OR o.rowid < s.rowid))").fetchall():
            keep = self.db.execute("SELECT id FROM submissions WHERE ref=? AND id<>? ORDER BY kind IS 'external', rowid "
                                   "LIMIT 1", (r["ref"], r["id"])).fetchone()["id"]
            for table, col in REFS["submissions"]:
                self.db.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (keep, r["id"]))
                self.db.execute(f"DELETE FROM {table} WHERE {col}=?", (r["id"],))
            self.db.execute("DELETE FROM submissions WHERE id=?", (r["id"],))
            self.db.execute("UPDATE ids SET local=? WHERE local=? AND writer='*'", (keep, r["id"]))
            self.db.execute("INSERT OR REPLACE INTO ids VALUES (?, '*', ?, 0)", (r["id"], keep))
            self._load_ids()

    def _run(self, t: str, op: str, e: dict, cols: set, log: str | None) -> int:
        if op == "ins":
            row = {k: v for k, v in (e.get("row") or {}).items() if k in cols}
            if log is not None and "writer" in cols and not row.get("writer"):
                row["writer"] = log.split("+", 1)[0]  # a row from before it had one: the log it is in
            verb = {"replace": "INSERT OR REPLACE", "ignore": "INSERT OR IGNORE"}.get(e.get("on"), "INSERT")
            sql = f"{verb} INTO {t} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})"
            try:
                return self.db.execute(sql, tuple(row.values())).rowcount
            except sqlite3.IntegrityError:
                if log is None:
                    raise
                return self._collide(t, row, log) if t in ENTITIES else 0
        where = e.get("where") or {}
        if not where or set(where) - cols:  # a where this kx cannot read would widen the change: skipped
            return 0
        cond, params = self._where(where)
        if op == "upd":
            values = {k: v for k, v in (e.get("set") or {}).items() if k in cols}
            if not values:
                return 0
            n = self.db.execute(f"UPDATE {t} SET {', '.join(f'{k}=?' for k in values)} WHERE {cond}",
                                (*values.values(), *params)).rowcount
            if not n and log is not None and t == "tasks" and values.get("claimed") and where.get("owner", 1) is None:
                n = self._claim(where["id"], values)
            return n
        if op == "del":
            return self.db.execute(f"DELETE FROM {t} WHERE {cond}", params).rowcount
        return 0

    def _claim(self, task: str, values: dict) -> int:
        """Two machines took an open task without seeing each other's claim: the one that took it earlier (by the
        clock, not by the order the logs replay in) has it."""
        row = self.db.execute("SELECT status, claimed FROM tasks WHERE id=?", (task,)).fetchone()
        if row is None or row["status"] not in ("active", "waiting") or not row["claimed"] \
                or str(values["claimed"]) >= row["claimed"]:
            return 0
        return self.db.execute(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in values)} WHERE id=?",
                               (*values.values(), task)).rowcount

    def _collide(self, t: str, row: dict, log: str) -> int:
        """Two machines made the same id (the same number and the same random name). Here this machine's row keeps
        it; of two other machines' rows, the one replayed first. The other shows as its full name (full_id); both
        are known by their full names in every log from then on, so each machine finds the same row."""
        old = self.db.execute(f"SELECT writer FROM {t} WHERE id=?", (row["id"],)).fetchone()
        w = row.get("writer") or log.split("+", 1)[0]
        if old is None or (old["writer"] or "") == w:
            return 0  # the same row twice (an import imported again)
        key, ow = row["id"], old["writer"] or ""
        self._id(key, ow, key)
        if self.mine(w) and not self.mine(ow):
            self._rename(t, key, full_id(key, ow))
            self._id(key, ow, full_id(key, ow))
            self._id(key, w, key)
            return self._apply({"t": t, "op": "ins", "row": row})
        self._id(key, w, full_id(key, w))
        return self._apply({"t": t, "op": "ins", "row": {**row, "id": full_id(key, w)}})

    def _id(self, key: str, writer: str, local: str):
        self.db.execute("INSERT INTO ids VALUES (?, ?, ?, NOT EXISTS (SELECT 1 FROM ids WHERE id=?)) "
                        "ON CONFLICT(id, writer) DO UPDATE SET local=excluded.local", (key, writer, local, key))
        self._load_ids()

    def _rename(self, t: str, old: str, new: str):
        """A row and every reference to it take another id."""
        self.db.execute(f"UPDATE {t} SET id=? WHERE id=?", (new, old))
        for table, col in REFS.get(t, ()):
            self.db.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))
        for table, col in JSON_REFS.get(t, ()):
            self.db.execute(f"UPDATE {table} SET {col}=replace({col}, ?, ?) WHERE {col} LIKE ?",
                            (f'"{old}"', f'"{new}"', f'%"{old}"%'))

    def _load_ids(self):
        full, by, first, out = {}, {}, {}, {}
        for r in self.db.execute("SELECT id, writer, local, first FROM ids ORDER BY rowid"):
            by[(r["id"], r["writer"])] = r["local"]
            if r["writer"] != "*":
                full[full_id(r["id"], r["writer"])] = r["local"]
                out[r["local"]] = full_id(r["id"], r["writer"])
                if r["first"]:
                    first[r["id"]] = r["local"]
        self._ids = {"full": full, "by": by, "first": first, "out": out}

    def local_id(self, key: str, writer: str | None = None) -> str:
        """What a row is here that `writer` calls `key` (its full name, or an id two rows have): the row it made,
        else the one replayed first."""
        ids = self._ids
        if key in ids["full"]:
            return ids["full"][key]
        return ids["by"].get((key, writer)) or ids["by"].get((key, "*")) or ids["first"].get(key) or key

    @staticmethod
    def _map(e: dict, f, skip_id: bool) -> dict:
        def fix(v):
            if not isinstance(v, str):
                return v
            if v.startswith("[") and v.endswith("]"):  # a JSON list of ids (depends, evidence)
                try:
                    items = json.loads(v)
                except ValueError:
                    return v
                if isinstance(items, list) and all(isinstance(x, str) for x in items):
                    got = [f(x) for x in items]
                    return json.dumps(got) if got != items else v
                return v
            return f(v)
        out = dict(e)
        for part in ("row", "set", "where"):
            if isinstance(e.get(part), dict):
                out[part] = {k: v if skip_id and part == "row" and k == "id" else fix(v) for k, v in e[part].items()}
        return out

    def _from_log(self, e: dict, writer: str) -> dict:
        """The ids an entry of `writer` names, as the rows are here (an inserted row keeps the id it was made with:
        a second one is a collision)."""
        return self._map(e, lambda v: self.local_id(v, writer), e.get("op") == "ins" and e.get("t") in ENTITIES)

    def _to_log(self, e: dict) -> dict:
        """A change made here, with each row that has a full name named by it."""
        out = self._ids["out"]
        return self._map(e, lambda v: out.get(v, v), False) if out else e

    # -- the cache ------------------------------------------------------------------------------------------------

    def _prepare(self):
        v = self.db.execute("PRAGMA user_version").fetchone()[0]
        if v > CACHE_VERSION:  # dropping it would make two kx versions take turns rebuilding
            raise KxError(f"{self.comp.db_path} is the cache of a newer kx (v{v}; this one knows v{CACHE_VERSION})",
                          fix="update kx (git pull in the kx repo)")
        if v != CACHE_VERSION:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("PRAGMA user_version").fetchone()[0] != CACHE_VERSION:  # not done meanwhile
                    keep = self._local_state()
                    for (name,) in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                   "AND name NOT LIKE 'sqlite_%'").fetchall():
                        self.db.execute(f"DROP TABLE {name}")
                    for stmt in SCHEMA:
                        self.db.execute(stmt)
                    self._cols = {}
                    self.db.execute(f"PRAGMA user_version={CACHE_VERSION}")
                    self._restore(keep)
                    self.rebuild(keep["columns"])
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
        self.catch_up()

    def _local_state(self) -> dict:
        """What only this cache has, no log: the workers, this machine's settings, the local columns."""
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out = {"workers": [], "settings": [], "columns": self._local_columns()}
        if "workers" in tables:
            out["workers"] = [dict(r) for r in self.db.execute("SELECT * FROM workers")]
        if "settings" in tables:
            out["settings"] = [dict(r) for r in self.db.execute(
                f"SELECT * FROM settings WHERE key IN ({','.join('?' * len(LOCAL_SETTINGS))})", LOCAL_SETTINGS)]
        return out

    def _local_columns(self) -> dict:
        out = {}
        for t, cols in LOCAL_COLUMNS.items():
            have = {c[1] for c in self.db.execute(f"PRAGMA table_info({t})")}
            if have >= {"id", *cols}:
                out[t] = [dict(r) for r in self.db.execute(f"SELECT id, {', '.join(cols)} FROM {t}")]
        return out

    def _restore(self, keep: dict):
        for t in ("workers", "settings"):
            cols = self._columns(t)
            for r in keep[t]:
                d = {k: v for k, v in r.items() if k in cols}
                self.db.execute(f"INSERT OR REPLACE INTO {t} ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})",
                                tuple(d.values()))

    def _logs(self) -> dict[str, int]:
        return {p.stem: p.stat().st_size for p in sorted(self.comp.log_dir.glob("*.jsonl"))}

    def _behind(self):
        """None (the cache has every log), a list of the logs that grew, or 'rebuild' (one shrank or went away)."""
        files = self._logs()
        applied = {r["log"]: r["size"] for r in self.db.execute("SELECT log, size FROM applied")}
        changed = [k for k in sorted(set(files) | set(applied)) if files.get(k) != applied.get(k)]
        if not changed:
            return None
        if all(files.get(k, -1) > applied.get(k, 0) for k in changed):
            return changed
        return "rebuild"

    def catch_up(self, force: bool = False) -> str | None:
        """Bring the cache up to the logs: what changed ('rebuilt', 'applied') or None. Logs that grew are applied in
        place when every new entry replays after everything already in the cache (the same as a rebuild would do);
        otherwise, or with `force` (a log was replaced), the cache is rebuilt."""
        if not force and self._behind() is None:
            return None
        with self.transaction():
            behind = "rebuild" if force else self._behind()  # again, under the lock: another process may have done it
            if behind is None:
                return None
            if behind != "rebuild":
                own = [log for log in behind if self.mine(log)]
                for log in own:  # a crash between this machine's log and the COMMIT, or a half-written line
                    self._catch_up_file(self.comp.log_dir / f"{log}.jsonl")
                if not self._apply_new([log for log in behind if log not in own]):
                    behind = "rebuild"
            if behind == "rebuild":
                self.rebuild()
                return "rebuilt"
            return "applied"
        return None

    def _apply_new(self, logs: list[str]) -> bool:
        """In a transaction: the new entries of other machines' logs, in replay order, when they all come after the
        last entry in the cache. False (nothing applied) when one would replay earlier: a rebuild is needed."""
        if not logs:
            return True
        applied = {r["log"]: r["size"] for r in self.db.execute("SELECT log, size FROM applied")}
        streams, ends = [], {}
        for log in logs:
            entries, ends[log] = read_log(self.comp.log_dir / f"{log}.jsonl", applied.get(log, 0))
            streams.append([(e.get("c") or 0, e.get("at") or "", log, i, k, e) for i, (k, e) in enumerate(entries)])
        first = min((s[0][:3] for s in streams if s), default=None)
        last = self._last()
        if first is not None and (last is None or first <= last):
            return False
        for c, at, log, _, k, e in heapq.merge(*streams, key=lambda x: x[:3]):
            self._apply(e, log, k)
            self._seen(c)
            self._advance((c, at, log))
        self.db.executemany("INSERT OR REPLACE INTO applied VALUES (?,?)", list(ends.items()))
        return True

    def _catch_up_own(self):
        if self.log_path.exists():
            self._catch_up_file(self.log_path)

    def _catch_up_file(self, p: Path):
        """In a transaction: lines of one of this machine's logs that are not in the cache (a crash between the log
        and the COMMIT), and a half-written last line, never committed, dropped."""
        size = p.stat().st_size
        row = self.db.execute("SELECT size FROM applied WHERE log=?", (p.stem,)).fetchone()
        done = row["size"] if row else 0
        if size == done:
            return
        entries, end = read_log(p, done)
        for k, e in entries:
            self._apply(e, p.stem, k)
            self._advance((e.get("c") or 0, e.get("at") or "", p.stem))
        self._seen(max((e.get("c") or 0 for _, e in entries), default=0))
        if end < size:
            with open(p, "r+b") as f:
                f.truncate(end)
        self.db.execute("INSERT OR REPLACE INTO applied VALUES (?,?)", (p.stem, end))

    def rebuild(self, keep: dict | None = None):
        """The cache from scratch: every log, merged in logical time (each log's own order kept). Needs the lock."""
        keep = self._local_columns() if keep is None else keep
        for t in LOGGED:
            if t == "settings":
                self.db.execute(f"DELETE FROM settings WHERE key NOT IN ({','.join('?' * len(LOCAL_SETTINGS))})",
                                LOCAL_SETTINGS)
            else:
                self.db.execute(f"DELETE FROM {t}")
        for t in ("ids", "entries", "sqlite_sequence"):
            self.db.execute(f"DELETE FROM {t}")
        self._load_ids()
        streams, sizes = [], {}
        for log, size in self._logs().items():
            path = self.comp.log_dir / f"{log}.jsonl"
            entries, sizes[log] = read_log(path)
            if sizes[log] < size and self.mine(log):  # a half-written line of a write that crashed: never committed
                with open(path, "r+b") as f:
                    f.truncate(sizes[log])
            streams.append([(e.get("c") or 0, e.get("at") or "", log, i, k, e) for i, (k, e) in enumerate(entries)])
        last, key = 0, None
        self.db.execute("DELETE FROM settings WHERE key='last_entry'")
        for c, at, log, _, k, e in heapq.merge(*streams, key=lambda x: x[:3]):
            self._apply(e, log, k)
            last, key = max(last, c), (c, at, log)
        self._seen(last)
        if key:
            self._advance(key)
        for t, rows in keep.items():
            cols = LOCAL_COLUMNS[t]
            for r in rows:
                self.db.execute(f"UPDATE {t} SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
                                (*(r[c] for c in cols), r["id"]))
        self.db.execute("DELETE FROM applied")
        self.db.executemany("INSERT INTO applied VALUES (?,?)", list(sizes.items()))

    # -- backups -------------------------------------------------------------------------------------------------

    @property
    def backup_dir(self) -> Path:
        return self.comp.store / "backups"

    def archive(self, path: Path, files=()) -> Path:
        """The logs (the ledger: the cache is rebuilt from them), and `files` as (file, name in the archive), in one
        tar.gz: a backup, `kx export`."""
        with tarfile.open(path, "w:gz") as tf:
            for p in sorted(self.comp.log_dir.glob("*.jsonl")):
                tf.add(p, f"log/{p.name}")
            for p, arc in files:
                tf.add(p, arc)
        return path

    def backup(self, dest_dir=None, tag: str = "manual") -> Path:
        d = Path(dest_dir).expanduser() if dest_dir else self.backup_dir
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path, i = d / f"log-{stamp}-{tag}.tar.gz", 1
        while path.exists():
            i += 1
            path = d / f"log-{stamp}-{tag}-{i}.tar.gz"
        self.archive(path)
        if tag != "manual":
            for old in sorted(d.glob(f"log-*-{tag}*.tar.gz"))[:-BACKUP_KEEP]:
                old.unlink(missing_ok=True)
        return path

    def maybe_backup(self):
        daily = sorted(self.backup_dir.glob("log-*-daily*.tar.gz")) if self.backup_dir.exists() else []
        if daily and datetime.now().timestamp() - daily[-1].stat().st_mtime < BACKUP_EVERY_S:
            return None
        if not any(self.comp.log_dir.glob("*.jsonl")):
            return None
        try:
            return self.backup(tag="daily")
        except (OSError, tarfile.TarError):
            return None

    # -- settings ------------------------------------------------------------------------------------------------

    def setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return loads(row["value"]) if row else default

    def set_setting(self, key: str, value):
        if key in LOCAL_SETTINGS:  # this machine's: not in the log
            if value is None:
                self.db.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, dumps(value)))
        elif value is None:
            self.delete("settings", key=key)
        elif self.setting(key) != value:
            self.insert("settings", {"key": key, "value": dumps(value)}, on="replace")

    # -- ids -----------------------------------------------------------------------------------------------------

    def resolve(self, ref, t: str = "runs") -> str:
        """An id from what someone typed: the id, its number (r12, r0012, 12; T4, #4), its name (fluent-sun) or
        both; for runs also 'latest' and 'best' (the best under the reference protocol). A number two machines
        both used means this machine's one, else asks."""
        what = {"runs": "run", "tasks": "task", "findings": "finding", "submissions": "submission"}[t]
        s = str(ref).strip()
        if t == "runs" and s.lower() in ("best", "latest"):
            if s.lower() == "best":
                from kx.runs.protocol import best_run
                row = best_run(self)
                if row is None:
                    raise KxError("no scored run under the reference protocol yet", fix="kx protocol list")
                return row
            row = self.one("SELECT id FROM runs ORDER BY n DESC, created DESC LIMIT 1")
            if row is None:
                raise KxError("there are no runs yet")
            return row["id"]
        if self.one(f"SELECT 1 FROM {t} WHERE id=?", (s,)):
            return s
        here = self._ids["full"].get(s)  # the full name of a row that shows here without its suffix
        if here and self.one(f"SELECT 1 FROM {t} WHERE id=?", (here,)):
            return here
        letter = ENTITIES[t]
        m = re.fullmatch(rf"(?:{letter}|#)?0*(\d+)", s, re.IGNORECASE)
        if m:
            rows = self.all(f"SELECT id, writer FROM {t} WHERE n=?", (int(m.group(1)),))
        else:
            m = re.fullmatch(rf"(?:{letter}0*(\d+)-)?([a-z]+(?:-[a-z]+)+)(?:-(\d+))?(?:~(\w+))?", s, re.IGNORECASE)
            if not m:
                raise KxError(f"not a {what} id: {ref!r}", fix=f"e.g. {entity_key(t, 12, 'fluent-sun')}, "
                                                               f"{letter}12 or fluent-sun")
            num = m.group(1) or m.group(3)
            rows = self.all(f"SELECT id, writer FROM {t} WHERE name=?" + (" AND n=?" if num else ""),
                            (m.group(2).lower(), int(num)) if num else (m.group(2).lower(),))
            if m.group(4):  # name~3f2a: the row of the writer the suffix is from
                rows = [r for r in rows if full_id("", r["writer"] or "")[1:] == m.group(4).lower()]
        if not rows:
            raise KxError(f"no {what} {s}", fix={"runs": "kx runs", "tasks": "kx task list --all",
                                                 "findings": "kx finding list --all", "submissions": "kx subs"}[t])
        if len(rows) > 1:
            here = [r for r in rows if r["writer"] in self.writers]
            if len(here) == 1:
                others = ", ".join(f"{r['id']} ({r['writer']})" for r in rows if r is not here[0])
                print(f"kx: {s} is this machine's {here[0]['id']}; also {others}. Others read ids in notes and "
                      f"findings: write the full id", file=sys.stderr)
                return here[0]["id"]
            raise KxError(f"{s} is ambiguous: {', '.join(r['id'] for r in rows)}", fix="use the full id")
        return rows[0]["id"]

    # -- runs ----------------------------------------------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.comp.runs_dir / run_id

    def get_run(self, run_id: str):
        row = self.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if row is None:
            raise KxError(f"no run {run_id}", fix="kx runs")
        return row

    def stale_runs(self, stale_after: int = STALE_AFTER_S) -> list[tuple]:
        """This machine's runs marked running whose process is gone or that stopped heart-beating. Runs on a remote
        host (kx exec --on) are never stale here: `kx remote sync` closes them; another machine's runs, that machine."""
        out, host = [], socket.gethostname()
        for r in self.all("SELECT * FROM runs WHERE status='running'"):
            if (self.run_dir(r["id"]) / "remote.json").exists() or (r["writer"] and not self.mine(r["writer"])):
                continue
            if r["host"] == host and pid_alive(r["pid"], r["pid_start"]) is False:
                out.append((r, f"process {r['pid']} is gone"))
                continue
            a = age_s(r["heartbeat"] or r["created"])
            if a is not None and a > stale_after:
                out.append((r, f"no heartbeat for {int(a // 60)} min"))
        return out

    def reap(self, stale_after: int = STALE_AFTER_S) -> list[tuple[str, str]]:
        """Close dead runs: done if they had recorded scores, else failed."""
        closed = []
        for r, why in self.stale_runs(stale_after):
            scored = self.one("SELECT COUNT(*) FROM scores WHERE run_id=?", (r["id"],))[0]
            status = "done" if scored else "failed"
            n = self.update("runs", {"status": status, "finished": now(), "error": None if scored else f"abandoned: {why}"},
                            id=r["id"], status="running")
            if n:
                if scored:
                    self.add_note(r["id"], f"kx reap: {why}; its scores were already recorded, kept as done")
                closed.append((r["id"], why))
        return closed

    # -- notes and tags ------------------------------------------------------------------------------------------

    def add_note(self, run_id: str, text: str):
        self.get_run(run_id)
        self.insert("notes", {"run_id": run_id, "ts": now(), "author": author(), "text": text})

    def notes(self, run_id: str):
        return self.all("SELECT * FROM notes WHERE run_id=? ORDER BY ts, id", (run_id,))

    def last_notes(self) -> dict[str, str]:
        return {r["run_id"]: r["text"] for r in self.all("SELECT run_id, text FROM notes ORDER BY ts, id")}

    def add_tags(self, run_id: str, tags):
        for t in norm_tags(tags):
            self.insert("tags", {"run_id": run_id, "tag": t}, on="ignore")

    def remove_tags(self, run_id: str, tags):
        for t in norm_tags(tags):
            self.delete("tags", run_id=run_id, tag=t)

    def tags(self, run_id: str) -> list[str]:
        return [r[0] for r in self.x("SELECT tag FROM tags WHERE run_id=? ORDER BY tag", (run_id,))]

    def unknown_tags(self, tags) -> list[str]:
        vocab = set(self.comp.tags)
        return [t for t in norm_tags(tags) if vocab and t not in vocab]

    # -- scores --------------------------------------------------------------------------------------------------

    def scores_for(self, run_id: str):
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
