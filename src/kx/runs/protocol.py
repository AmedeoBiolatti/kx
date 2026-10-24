"""Evaluation protocols: the unit of score comparability.

Two scores are comparable iff they were produced under the same protocol id
(``name@fingerprint``). The fingerprint hashes the protocol spec, so changing
anything that affects the score (folds, metric, opponent pool, data version,
number of games...) yields a new id, and kx refuses to rank across ids.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field

from kx.ledger.config import KxError
from kx.ledger.store import Store, dumps, loads, now, parse_ts

# Leaderboard scores are recorded as scores under these built-in protocols.
KAGGLE_PUBLIC = "kaggle:public"
KAGGLE_PRIVATE = "kaggle:private"


NOISE = ("independent", "cv")


@dataclass(frozen=True)
class Protocol:
    """How a score was produced.

    Put in ``spec`` everything that, if changed, makes scores incomparable:
    ``Protocol("cv5", metric="top1", data=kx.data_version(), folds="v2", seeds=3)``.

    ``noise`` says how to read the breakdown when estimating noise:
    ``"independent"`` (default: per-opponent / per-seed / disjoint-holdout scores) or
    ``"cv"`` (k-fold scores, which are correlated through overlapping training sets; kx then
    inflates the standard error with the Nadeau-Bengio correction).
    """

    name: str
    spec: dict = field(default_factory=dict)
    direction: str | None = None  # defaults to comp.yaml metric.direction
    description: str = ""
    noise: str = "independent"

    def __init__(self, name: str, *, direction: str | None = None, description: str = "",
                 noise: str = "independent", **spec):
        if "@" in name or name.startswith("kaggle:"):
            raise ValueError(f"invalid protocol name {name!r}")
        if direction not in (None, "max", "min"):
            raise ValueError(f"direction must be 'max' or 'min', got {direction!r}")
        if noise not in NOISE:
            raise ValueError(f"noise must be one of {NOISE}, got {noise!r}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "spec", spec)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "noise", noise)

    def __hash__(self):
        return hash(self.id)

    @property
    def fingerprint(self) -> str:
        canon = {"direction": self.direction, "spec": self.spec}
        if self.noise != "independent":  # only when set, so default fingerprints stay stable
            canon["noise"] = self.noise
        return hashlib.sha256(json.dumps(canon, sort_keys=True, default=str).encode()).hexdigest()[:8]

    @property
    def id(self) -> str:
        return f"{self.name}@{self.fingerprint}"


def spec_diff(a: dict, b: dict) -> list[str]:
    out = []
    for k in sorted(set(a) | set(b)):
        if k not in a:
            out.append(f"+{k}={b[k]!r}")
        elif k not in b:
            out.append(f"-{k} (was {a[k]!r})")
        elif a[k] != b[k]:
            out.append(f"{k}: {a[k]!r} -> {b[k]!r}")
    return out


def register(store: Store, proto: Protocol) -> str:
    """Insert the protocol if new; warn loudly when a name gets a new fingerprint."""
    direction = proto.direction or store.comp.direction
    if proto.direction is None:
        # resolve the default so the fingerprint does not silently depend on comp.yaml later
        proto = Protocol(proto.name, direction=direction, description=proto.description, noise=proto.noise,
                         **proto.spec)
    pid = proto.id
    if store.x("SELECT 1 FROM protocols WHERE id=?", (pid,)).fetchone():
        return pid
    prev = store.x(
        "SELECT * FROM protocols WHERE name=? ORDER BY created DESC LIMIT 1", (proto.name,)
    ).fetchone()
    store.x(
        "INSERT OR IGNORE INTO protocols (id, name, fingerprint, spec, direction, description, created, noise)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (pid, proto.name, proto.fingerprint, dumps(proto.spec), direction, proto.description, now(), proto.noise),
    )
    if prev is not None:
        n = store.x("SELECT COUNT(*) FROM scores WHERE protocol_id=?", (prev["id"],)).fetchone()[0]
        diff = spec_diff(loads(prev["spec"]), proto.spec)
        if prev["direction"] != direction:
            diff.append(f"direction: {prev['direction']} -> {direction}")
        if prev["noise"] != proto.noise:
            diff.append(f"noise: {prev['noise']} -> {proto.noise}")
        print(
            f"kx: protocol '{proto.name}' changed -> new id {pid} (was {prev['id']}).\n"
            f"    diff: {'; '.join(diff)}\n"
            f"    Scores under {pid} are NOT comparable with the {n} score(s) under {prev['id']}.\n"
            f"    Re-score the runs you compare against under {pid}.",
            file=sys.stderr,
        )
    else:
        ref = reference(store)
        note = ("" if ref.get("name") in (None, proto.name) else
                f" It is a side evaluation: runs are ranked under the reference '{ref['name']}' "
                f"(`kx protocol use {proto.name}` to rank under it instead).")
        print(f"kx: new protocol {pid}.{note}", file=sys.stderr)
    return pid


def ensure_kaggle_protocols(store: Store):
    mode = store.comp.scoring
    for pid, desc in ((KAGGLE_PUBLIC, mode.public), (KAGGLE_PRIVATE, mode.private)):
        store.x(
            "INSERT OR IGNORE INTO protocols (id, name, fingerprint, spec, direction, description, created)"
            " VALUES (?,?,?,?,?,?,?)",
            (pid, pid, "kaggle", dumps({}), store.comp.direction, desc, now()),
        )


def protocol_name(pid: str) -> str:
    return pid.rsplit("@", 1)[0]


def superseded(store: Store) -> dict[str, str]:
    """{old protocol id: newest id with the same name}, for names that changed spec."""
    rows = store.x(
        "SELECT id, name FROM protocols WHERE id NOT LIKE 'kaggle:%' ORDER BY created DESC, rowid DESC"
    ).fetchall()
    newest, out = {}, {}
    for r in rows:
        if r["name"] in newest:
            out[r["id"]] = newest[r["name"]]
        else:
            newest[r["name"]] = r["id"]
    return out


RECENT_DAYS = 14


def reference(store: Store) -> dict:
    """The protocol runs are ranked under: {"name", "id" (newest fingerprint), "source": set | comp.yaml | inferred}.
    Without one set, the protocol name with most scores in the last 14 days (not a Kaggle one)."""
    name, source = store.setting("reference"), "set"
    if not name and store.comp.reference:
        name, source = store.comp.reference, "comp.yaml"
    if not name:
        cutoff = (parse_ts(now()).timestamp() - RECENT_DAYS * 86400)
        counts: dict[str, int] = {}
        for r in store.all("SELECT p.name, s.created FROM scores s JOIN protocols p ON p.id=s.protocol_id "
                           "WHERE s.protocol_id NOT LIKE 'kaggle:%'"):
            t = parse_ts(r["created"])
            if t and t.timestamp() >= cutoff:
                counts[r["name"]] = counts.get(r["name"], 0) + 1
        if not counts:
            return {"name": None, "id": None, "source": None}
        name, source = max(counts, key=lambda k: (counts[k], k)), "inferred"
    row = store.one("SELECT id FROM protocols WHERE name=? ORDER BY created DESC, rowid DESC LIMIT 1", (name,))
    return {"name": name, "id": row["id"] if row else None, "source": source}


def use(store: Store, name: str) -> dict:
    name = protocol_name(name.strip())
    if not store.one("SELECT 1 FROM protocols WHERE name=?", (name,)):
        known = [r["name"] for r in store.all("SELECT DISTINCT name FROM protocols WHERE id NOT LIKE 'kaggle:%' ORDER BY name")]
        raise KxError(f"no protocol named {name!r}", fix="known: " + (", ".join(known) or "none yet"))
    store.set_setting("reference", name)
    return reference(store)


def ranking(store: Store, pid: str, limit: int | None = None, include_discarded: bool = False) -> list:
    """Done runs scored under pid, best first."""
    p = store.protocol(pid)
    order = "DESC" if p["direction"] == "max" else "ASC"
    hide = "" if include_discarded else " AND r.id NOT IN (SELECT run_id FROM tags WHERE tag='discarded')"
    sql = (f"SELECT r.*, s.value, s.se, s.n, s.breakdown, s.protocol_id FROM scores s JOIN runs r ON r.id=s.run_id "
           f"WHERE s.protocol_id=? AND r.status='done'{hide} ORDER BY s.value {order}, r.id")
    rows = store.all(sql + (" LIMIT ?" if limit else ""), (pid, limit) if limit else (pid,))
    return rows


def best_run(store: Store) -> int | None:
    ref = reference(store)
    if not ref["id"]:
        return None
    rows = ranking(store, ref["id"], 1)
    return rows[0]["id"] if rows else None
