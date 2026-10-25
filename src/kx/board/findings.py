"""Findings: conclusions backed by runs, with a status and topic tags."""

from __future__ import annotations

from kx.ledger.config import KxError
from kx.ledger.store import Store, author, dumps, loads, norm_tags, now

STATUSES = ("open", "withdrawn", "superseded")
LIVE = ("open", "confirmed")  # confirmed: older ledgers


def _tags(store: Store, key: str) -> list[str]:
    return [r[0] for r in store.x("SELECT tag FROM finding_tags WHERE finding_id=? ORDER BY tag", (key,))]


def get(store: Store, ref) -> dict:
    key = store.resolve(ref, "findings")
    d = dict(store.one("SELECT * FROM findings WHERE id=?", (key,)))
    d["evidence"] = loads(d["evidence"]) or []
    d["tags"] = _tags(store, key)
    return d


def add(store: Store, text: str, evidence=(), tags=(), task: str | None = None) -> dict:
    runs = [store.resolve(e) for e in evidence or []]
    with store.transaction():
        key = store.new("findings", {"ts": now(), "author": author(), "text": text.strip(), "evidence": dumps(runs),
                                     "task": task})
        for t in norm_tags(tags):
            store.insert("finding_tags", {"finding_id": key, "tag": t}, on="ignore")
    return get(store, key)


def review(store: Store, ref, status: str, note: str = "", by=None) -> dict:
    f = get(store, ref)
    if status not in STATUSES[1:]:
        raise KxError(f"status must be one of {', '.join(STATUSES[1:])}")
    if status == "superseded":
        if by is None:
            raise KxError("superseding needs the finding that replaces it", fix=f"kx finding supersede {f['id']} --by F<n>")
        by = get(store, by)["id"]
    store.update("findings", {"status": status, "reviewed_by": author(), "reviewed": now(), "review_note": note or None,
                              "superseded_by": by}, id=f["id"])
    return get(store, f["id"])


def listing(store: Store, all_: bool = False, tag: str | None = None, limit: int | None = None) -> list[dict]:
    where, params = [], []
    if not all_:
        where.append(f"status IN ({','.join('?' * len(LIVE))})")
        params += LIVE
    if tag:
        where.append("id IN (SELECT finding_id FROM finding_tags WHERE tag=?)")
        params.append(norm_tags([tag])[0])
    sql = "SELECT id FROM findings" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY n DESC, ts DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [get(store, r["id"]) for r in store.all(sql, params)]
