"""`kx brief` (and `kx` alone): what happened since the person last looked, and what needs them.

kx status says how things are; the brief says what changed, so the person does not piece it together from runs,
compare and show: runs that ended and what they came to, a new best, tasks finished with their outcome, findings,
submissions sent and scored, workers that ended, what was spent. "Last looked" is per person and machine (the local
setting `seen`: looking is not worth a line in the shared log): `kx` on a terminal, the dashboard's "Got it", and the brief a Claude Code session starts with
(`kx brief --hook`) move it; an agent reading `kx brief` does not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kx.guard import budgets
from kx.ledger.store import Store, author, now, parse_ts
from kx.runs.protocol import ranking, reference
from kx.views.text import ago, bold, dim, dur, num, score, trunc

DEFAULT_HOURS = 24  # someone who never looked: the last day


def last_seen(store: Store, who: str | None = None) -> str:
    return (store.setting("seen") or {}).get(who or author()) or (
        datetime.now(timezone.utc) - timedelta(hours=DEFAULT_HOURS)).isoformat(timespec="seconds")


def mark(store: Store, who: str | None = None) -> str:
    ts = now()
    store.set_setting("seen", {**(store.setting("seen") or {}), who or author(): ts})
    return ts


def changes(store: Store, since: str) -> dict:
    """What the ledger recorded since `since`."""
    ref = reference(store)
    ended = [dict(r) for r in store.all("SELECT id, status, hypothesis, author, task, error FROM runs "
                                        "WHERE finished >= ? ORDER BY n, id", (since,))]
    out = {"runs": {"done": sum(r["status"] == "done" for r in ended), "failed": [
        {"run": r["id"], "hypothesis": r["hypothesis"], "error": trunc((r["error"] or "").strip().splitlines()[-1]
                                                                            if r["error"] else "", 160)}
        for r in ended if r["status"] == "failed"][:5]}, "reference": ref["id"], "scored": [], "best": None}
    if ref["id"]:
        board = ranking(store, ref["id"])
        rank = {r["id"]: i + 1 for i, r in enumerate(board)}
        new = {r["id"] for r in ended}
        out["scored"] = [{"run": r["id"], "value": r["value"], "se": r["se"], "rank": rank[r["id"]], "of": len(board),
                          "hypothesis": r["hypothesis"]} for r in board if r["id"] in new][:5]
        if board and board[0]["id"] in new:
            before = next((r for r in board if r["id"] not in new), None)
            out["best"] = {"run": board[0]["id"], "value": board[0]["value"], "se": board[0]["se"],
                           "was": {"run": before["id"], "value": before["value"]} if before else None}
    out["tasks"] = [{"id": t["id"], "title": t["title"], "status": t["status"], "result": t["result"],
                     "outcome": t["outcome"], "owner": t["owner"]}
                    for t in store.all("SELECT * FROM tasks WHERE status IN ('done','dropped') AND updated >= ? "
                                       "ORDER BY updated", (since,))]
    out["tasks_added"] = [{"id": t["id"], "title": t["title"], "by": t["created_by"]}
                          for t in store.all("SELECT * FROM tasks WHERE created >= ? ORDER BY n, id", (since,))]
    out["findings"] = [{"id": f["id"], "text": f["text"], "author": f["author"]}
                       for f in store.all("SELECT * FROM findings WHERE ts >= ? ORDER BY n, id", (since,))]
    out["findings_reviewed"] = [{"id": f["id"], "status": f["status"], "note": f["review_note"]}
                                for f in store.all("SELECT * FROM findings WHERE reviewed >= ? AND ts < ? "
                                                   "AND status IN ('withdrawn','superseded') ORDER BY n, id", (since, since))]
    first = "(SELECT MIN(ts) FROM submission_scores x WHERE x.submission_id=s.id)"  # when Kaggle first scored it
    out["submissions"] = [{"id": s["id"], "run": s["run_id"], "status": s["status"],
                           "public": s["public"] if (s["scored"] or "") >= since else None,
                           "sent": (s["created"] or "") >= since, "error": s["error"]}
                          for s in store.all(f"SELECT s.*, {first} scored FROM submissions s WHERE s.created >= ? "
                                             f"OR {first} >= ? ORDER BY s.n, s.id", (since, since))]
    out["workers"] = [{"worker": w["agent"], "task": w["task"], "code": w["code"], "stop": w["stop"],
                       "tokens": (w["tokens_in"] or 0) + (w["tokens_out"] or 0)}
                      for w in store.all("SELECT * FROM workers WHERE finished >= ? ORDER BY finished", (since,))]
    out["used"] = {r["metric"]: r["total"] for r in store.all(
        "SELECT metric, SUM(amount) total FROM usage WHERE ts >= ? GROUP BY metric", (since,))}
    return out


def brief(store: Store, since: str | None = None) -> dict:
    from kx.views.status import needs_you
    since = since or last_seen(store)
    return {"competition": store.comp.slug, "since": since, "person": author(), "changes": changes(store, since),
            "needs_you": needs_you(store)}


def empty(c: dict) -> bool:
    return not (c["runs"]["done"] or c["runs"]["failed"] or c["tasks"] or c["tasks_added"] or c["findings"]
                or c["findings_reviewed"] or c["submissions"] or c["workers"] or c["used"])


def render(b: dict, needs: bool = True) -> str:
    c = b["changes"]
    when = parse_ts(b["since"])
    head = bold("SINCE YOU LOOKED") + dim(f"  {ago(b['since'])} ago" + (f" ({when.astimezone():%a %H:%M})" if when else ""))
    out = [head]
    if empty(c):
        out.append(dim("  nothing new"))
    rows = []
    r = c["runs"]
    if r["done"] or r["failed"]:
        line = f"{r['done'] + len(r['failed'])} ended" + (f", {len(r['failed'])} failed" if r["failed"] else "")
        if c["best"]:
            was = c["best"]["was"]
            line += (f" · new best {c['best']['run']} {score(c['best']['value'], c['best']['se'])}"
                     + (f" (was {num(was['value'])}, {was['run']})" if was else ""))
        rows.append(("runs", line))
        if c["scored"]:
            rows.append(("", " · ".join(f"{x['run']} {num(x['value'])} #{x['rank']}/{x['of']}" for x in c["scored"])))
        for f in r["failed"][:3]:
            rows.append(("", f"{f['run']} failed: {f['error'] or trunc(f['hypothesis'], 80)}"))
    for t in c["tasks"]:
        rows.append(("tasks" if not any(x[0] == "tasks" for x in rows) else "",
                     f"{t['id']} {t['status']}" + (f", {t['result']}" if t["result"] else "")
                     + f": {trunc(t['outcome'] or t['title'], 140)}"))
    if c["tasks_added"]:
        rows.append(("new tasks", ", ".join(f"{t['id']} {trunc(t['title'], 50)}" for t in c["tasks_added"][:5])
                     + (f" +{len(c['tasks_added']) - 5}" if len(c["tasks_added"]) > 5 else "")))
    for f in c["findings"][:5]:
        rows.append(("findings" if not any(x[0] == "findings" for x in rows) else "", f"{f['id']} {trunc(f['text'], 140)}"))
    if c["findings_reviewed"]:
        rows.append(("" if any(x[0] == "findings" for x in rows) else "findings",
                     ", ".join(f"{f['id']} {f['status']}" for f in c["findings_reviewed"])))
    subs = []
    for s in c["submissions"]:
        what = f"{s['id']}" + (f" ({s['run']})" if s["run"] else "")
        if s["public"] is not None:
            subs.append(f"{what} scored {num(s['public'])}")
        elif s["status"] == "error":
            subs.append(f"{what} failed: {trunc(s['error'], 60)}")
        elif s["sent"]:
            subs.append(f"{what} sent")
    if subs:
        rows.append(("submissions", " · ".join(subs)))
    if c["workers"]:
        rows.append(("workers", " · ".join(
            f"{w['worker']} on {w['task']}" + (f" stopped: {trunc(w['stop'], 50)}" if w["stop"] else
                                               " ended" if not w["code"] else f" exit {w['code']}") for w in c["workers"])))
    if c["used"]:
        rows.append(("used", " · ".join(budgets.fmt(m, v) + f" {m.replace('_', ' ')}" for m, v in sorted(c["used"].items()))))
    out += [f"  {k:<12} {v}" for k, v in rows]
    if needs:
        out += ["", needs_text(b["needs_you"])]
    return "\n".join(out).rstrip() + "\n"


def needs_text(items: list[dict]) -> str:
    dec = [x for x in items if x["who"] == "decision"]
    chores = [x for x in items if x["who"] == "chore"]
    advice = [x for x in items if x["who"] == "advice"]
    if not items:
        return bold("NEEDS YOU") + dim("  nothing")
    counts = [f"{len(dec)} decision{'s' if len(dec) != 1 else ''}"] + ([f"{len(chores)} chore{'s' if len(chores) != 1 else ''}"]
                                                                       if chores else []) + ([f"{len(advice)} to know"] if advice else [])
    out = [bold("NEEDS YOU") + dim("  " + " · ".join(counts))]
    for x in dec:
        out.append(f"  {x['what']}" + dim(f" · {dur(x['age_s'])}" if x.get("age_s") is not None else ""))
        out.append(dim(f"      {how(x)}"))
    for x in chores:
        out.append(f"  {x['what']}" + dim(f"  ({x['fix']}: anyone may, the agent you talk to included)"))
    for x in advice:
        out.append(dim(f"  {x['what']}  ({x['fix']})"))
    return "\n".join(out)


def how(x: dict) -> str:
    """The commands that settle a decision, as an agent or a person types them."""
    acts = x.get("actions") or []
    parts = []
    for a in acts:
        cmd = "kx " + " ".join(a["argv"]) + (' "..."' if a.get("input") else "")
        parts.append(cmd)
    return " · ".join(parts) or x["fix"]


def hook_text(b: dict) -> str:
    """What a Claude Code session starts with (the SessionStart hook): the brief, and what to do with it."""
    head = (f"kx brief for {b['competition']}, for the person you are working with: what happened since they last "
            "looked, and what needs them. In your first reply, tell them in a few lines what matters here; offer to "
            "settle the decisions (ask before `kx approve T<n> --yes`; `kx answer T<n> \"...\"` with their words), and "
            "do the chores yourself.")
    return head + "\n\n" + render(b)
