"""Findings: conclusions backed by runs, with a status and topic tags."""

from __future__ import annotations

import sys

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


def add(store: Store, text: str, evidence=(), tags=(), task: str | None = None, partial: bool = False) -> dict:
    """A finding. Its evidence must be runs that have ended (with partial, running ones too: the finding then says it
    rests on results not final yet); a failed run is evidence only of its failure, and kx says so."""
    runs = [store.resolve(e) for e in evidence or []]
    rows = {r: store.get_run(r) for r in runs}
    going = [r for r, row in rows.items() if row["status"] == "running"]
    if going and not partial:
        raise KxError(f"{', '.join(going)} {'is' if len(going) == 1 else 'are'} still running: its results are not final",
                      fix=f"kx wait {' '.join(going)}, then add the finding (or --partial: a finding on partial results)")
    if going:
        text = f"{text.strip()} [partial: {', '.join(going)} still running]"
    if failed := [r for r, row in rows.items() if row["status"] == "failed"]:
        print(f"kx: note: {', '.join(failed)} failed: as evidence {'it shows' if len(failed) == 1 else 'they show'} "
              "the failure, not a result", file=sys.stderr)
    if not runs:
        print("kx: note: a finding without evidence: add the runs it rests on (--evidence rN ...) if there are any",
              file=sys.stderr)
    with store.transaction():
        key = store.new("findings", {"ts": now(), "author": author(), "text": text.strip(), "evidence": dumps(runs),
                                     "task": task})
        for t in norm_tags(tags):
            store.insert("finding_tags", {"finding_id": key, "tag": t}, on="ignore")
    return get(store, key)


def shaken(store: Store) -> list[dict]:
    """Live findings resting on a run that failed after the finding was written (it was running then)."""
    out = []
    for f in store.all(f"SELECT id, ts, evidence FROM findings WHERE status IN ({','.join('?' * len(LIVE))})", LIVE):
        for r in loads(f["evidence"]) or []:
            row = store.one("SELECT status, finished FROM runs WHERE id=?", (r,))
            if row and row["status"] == "failed" and (row["finished"] or "") >= f["ts"]:
                out.append({"finding": f["id"], "run": r})
    return out


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
