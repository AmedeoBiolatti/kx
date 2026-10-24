"""Findings: conclusions backed by runs, with a status and topic tags."""

from __future__ import annotations

from kx.ledger.config import KxError
from kx.ledger.store import Store, author, dumps, loads, norm_tags, now

STATUSES = ("open", "withdrawn", "superseded")
LIVE = ("open", "confirmed")  # confirmed: older ledgers


def fid(n: int) -> str:
    return f"F{n}"


def parse_id(ref) -> int:
    s = str(ref).strip().upper().lstrip("#")
    if s.startswith("F"):
        s = s[1:]
    if not s.isdigit():
        raise KxError(f"not a finding id: {ref!r}", fix="use e.g. F3 (kx finding list)")
    return int(s)


def _tags(store: Store, n: int) -> list[str]:
    return [r[0] for r in store.x("SELECT tag FROM finding_tags WHERE finding_id=? ORDER BY tag", (n,))]


def get(store: Store, ref) -> dict:
    n = parse_id(ref)
    row = store.one("SELECT * FROM findings WHERE id=?", (n,))
    if row is None:
        raise KxError(f"no finding F{n}", fix="kx finding list --all")
    d = dict(row)
    d["evidence"] = loads(d["evidence"]) or []
    d["tags"] = _tags(store, n)
    return d


def add(store: Store, text: str, evidence=(), tags=(), task: int | None = None) -> dict:
    runs = [store.resolve(e) for e in evidence or []]
    for r in runs:
        store.get_run(r)
    with store.transaction():
        n = store.x("INSERT INTO findings (ts, author, text, evidence, task) VALUES (?,?,?,?,?)",
                    (now(), author(), text.strip(), dumps(runs), task)).lastrowid
        for t in norm_tags(tags):
            store.x("INSERT OR IGNORE INTO finding_tags VALUES (?,?)", (n, t))
    return get(store, n)


def review(store: Store, ref, status: str, note: str = "", by: int | None = None) -> dict:
    f = get(store, ref)
    if status not in STATUSES[1:]:
        raise KxError(f"status must be one of {', '.join(STATUSES[1:])}")
    if status == "superseded":
        if by is None:
            raise KxError("superseding needs the finding that replaces it", fix=f"kx finding supersede F{f['id']} --by F<n>")
        get(store, by)
    store.x("UPDATE findings SET status=?, reviewed_by=?, reviewed=?, review_note=?, superseded_by=? WHERE id=?",
            (status, author(), now(), note or None, by, f["id"]))
    return get(store, f["id"])


def listing(store: Store, all_: bool = False, tag: str | None = None, limit: int | None = None) -> list[dict]:
    where, params = [], []
    if not all_:
        where.append(f"status IN ({','.join('?' * len(LIVE))})")
        params += LIVE
    if tag:
        where.append("id IN (SELECT finding_id FROM finding_tags WHERE tag=?)")
        params.append(norm_tags([tag])[0])
    sql = "SELECT id FROM findings" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [get(store, r["id"]) for r in store.all(sql, params)]
