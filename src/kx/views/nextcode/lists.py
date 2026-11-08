"""The Tasks, Findings and Submissions panels: the board's lists, filtered, and one task or submission opened (by
its row, its id, or a row of the Overview's) with what `kx task show` and `kx subs S` have. With API 4 the one opened
is beside the list, which keeps its place."""

from __future__ import annotations

from kx.ledger.config import KxError
from kx.views.nextcode import Ctx, Go, footer, header, run_kx, states
from kx.views.nextcode.widgets import (HOST, button, chart, checkbox, col, esc, label, md, row, select, sidebar, table,
                                       text_input, trunc)

CARD = 600  # px: the task or submission opened, beside the list (API 4)


def picked(ctx: Ctx, table_id: str, open_id: str, current, shown: list):
    """What to open after this event: a row, an id typed (T12, 12 or a whole id: kx resolves it), one sent from another
    page, Close, or what was open."""
    if ctx.clicked("close"):
        return None
    if (key := ctx.row(table_id, shown)) is not None:
        return key
    if isinstance(sent := ctx.args.get("open"), str) and sent:
        return sent
    if typed := ctx.submitted(open_id):
        return typed
    return current


def words_match(text: str, q: str) -> bool:
    t = text.lower()
    return all(w in t for w in q.lower().split())


def page(listing: list, card: list) -> dict:
    """The list, and what is open: beside it (API 4, the list keeping its place), else under it."""
    if card and HOST.api >= 4:
        return sidebar("root", card, listing, width=CARD, right=True)
    return col("root", *listing[:-1], col("card", *card) if card else None, listing[-1], gap=12)


# --- tasks


def tasks(ctx: Ctx) -> tuple[dict, dict]:
    from kx.views.text import ago
    state = {"all": ctx.value("all", False), "q": ctx.value("q", ""), "task": ctx.state.get("task"), "shown": []}
    state["task"] = picked(ctx, "tasks", "open", state["task"], ctx.state.get("shown") or [])
    if ctx.clicked("train-task") and state["task"]:  # its runs side by side, the newest
        raise Go("training", runs=[r["id"] for r in run_kx(["task", "show", state["task"]]).get("runs") or []][-6:], only=True)
    rows = run_kx(["task", "list"] + (["--all"] if state["all"] else []))
    hit = [t for t in rows if words_match(" ".join(str(t.get(k) or "") for k in ("id", "title", "body", "status", "owner", "outcome"))
                                          , state["q"])]
    shown = hit[: 40 if ctx.tight else 120]
    state["shown"] = [t["id"] for t in shown]
    sel = next((i for i, t in enumerate(shown) if t["id"] == state["task"]), None)
    filters = row("filters", checkbox("all", "done and dropped too", state["all"]),
                  text_input("q", "Words", state["q"], "in the title, description, outcome (Enter)"),
                  text_input("open", "Open a task", "", "T12 (Enter)"), gap=12)
    beside = bool(state["task"]) and HOST.api >= 4
    cols = {"task": 50, "title": 320, "state": 110, "p": 30, "owner": 90, "updated": 80, "latest": 0}
    names = [k for k in cols if not beside or k in ("task", "title", "state")]
    if beside:
        cols["title"] = 0
    cells = lambda t: {"task": t["id"], "title": t["title"], "state": states.task(t), "p": t.get("priority", ""),  # noqa: E731
                       "owner": t.get("owner") or "", "updated": f"{ago(t['updated'])} ago",
                       "latest": (t.get("last_note") or {}).get("text") or t.get("outcome") or ""}
    listing = table("tasks", names, [[c[k] for k in names] for c in map(cells, shown)], widths=[cols[k] for k in names],
                    selectable=True, selected=sel, max_height=1200 if beside else 480) \
        if shown else label("tasks-empty", "No task matches." if rows else "No tasks.")
    counts = {s: sum(t["status"] == s for t in rows) for s in ("active", "waiting", "open")}
    head = header("Tasks", " · ".join(f"{n} {s}" for s, n in counts.items()) + (f" · {len(rows)} in all" if state["all"] else "")
                  + (" · click one to open it" if HOST.api >= 4 and shown and not state["task"] else ""))
    card = task_card(state["task"], ctx.tight) if state["task"] else []
    return page([head, filters, listing, footer()], card), state


def task_card(task: str, tight: bool) -> list[dict]:
    from kx.views.text import ago
    try:
        t = run_kx(["task", "show", task])
    except KxError as e:
        return [row("card-head", md("card-title", f"**{esc(task)}**: {esc(e)}"), button("close", "Close"))]
    meta = [states.task(t)[0], f"p{t['priority']}", t.get("owner"), t.get("waiting_on") and f"waits on {esc(t['waiting_on'])}",
            t.get("depends") and "depends on " + ", ".join(str(d) for d in t["depends"]), t.get("branch") and f"branch {esc(t['branch'])}",
            f"updated {ago(t['updated'])} ago"]
    out = [row("card-head", md("card-title", f"### {t['id']} · {esc(t['title'])}\n\n" + " · ".join(m for m in meta if m)),
               button("train-task", "Its runs in Training") if t.get("runs") else None, button("close", "Close"))]
    if t.get("body"):
        out.append(md("card-body", f"**Description**\n\n{esc(trunc(t['body'], 3000))}"))
    if t.get("outcome"):
        out.append(md("card-outcome", f"**Outcome**\n\n{esc(trunc(t['outcome'], 2000))}"))
    if t.get("notes"):
        out.append(md("card-notes", "**Progress notes**\n\n" + "\n".join(
            f"- {ago(n['ts'])} ago · {esc(n.get('author') or '')}: {esc(trunc(n['text'], 600))}" for n in t["notes"][-(4 if tight else 15):])))
    if t.get("runs"):
        out.append(table("card-runs", ["run", "status", "hypothesis"],
                         [[r["id"], states.run(r), r.get("hypothesis") or ""] for r in t["runs"][-60:]], widths=[64, 90, 0]))
    if t.get("findings"):
        out.append(md("card-findings", "**Findings**\n\n" + "\n".join(f"- **{f['id']}** · {f['status']} · {esc(trunc(f['text'], 400))}"
                                                                      for f in t["findings"][:20])))
    return out


# --- findings


def findings(ctx: Ctx) -> tuple[dict, dict]:
    from kx.views.text import ago
    state = {"all": ctx.value("all", False), "tag": ctx.value("tag", "-"), "q": ctx.value("q", "")}
    rows = run_kx(["finding", "list"] + (["--all"] if state["all"] else []))
    tag_counts = {}
    for f in rows:
        for t in f.get("tags") or []:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    hit = [f for f in rows if (state["tag"] == "-" or state["tag"] in (f.get("tags") or []))
           and words_match(f"{f['id']} {f['text']} {' '.join(f.get('tags') or [])}", state["q"])]
    shown = hit[: 40 if ctx.tight else 120]
    filters = row("filters", checkbox("all", "reviewed and superseded too", state["all"]),
                  select("tag", "Tag", state["tag"], [("-", "any tag"), *((t, f"{t} · {n}") for t, n in
                                                                          sorted(tag_counts.items(), key=lambda x: -x[1]))]),
                  text_input("q", "Words", state["q"], "in the text (Enter)"), gap=12)

    def entry(f):
        bits = [f"**{f['id']}**", f["status"], ", ".join(f.get("tags") or []), f"{ago(f['ts'])} ago", esc(f.get("author") or ""),
                f.get("task"), f.get("evidence") and "runs " + ", ".join(f["evidence"][:8]),
                f.get("superseded_by") and f"superseded by {f['superseded_by']}"]
        return " · ".join(b for b in bits if b) + f"\n\n{esc(trunc(f['text'], 280 if ctx.tight else 700))}"
    blocks = [md(f"findings-{i // 20}", "\n\n---\n\n".join(entry(f) for f in shown[i:i + 20])) for i in range(0, len(shown), 20)]
    more = label("findings-more", f"{len(hit) - len(shown)} more: filter to see them") if len(shown) < len(hit) else None
    body = [header("Findings", f"{len(hit)} of {len(rows)} {'findings' if state['all'] else 'open findings'}"), filters,
            *(blocks or [label("findings-empty", "No finding matches." if rows else "No findings yet.")]), more, footer()]
    return col("root", *body, gap=12), state


# --- submissions


def subs(ctx: Ctx) -> tuple[dict, dict]:
    from kx.views.text import ago, num
    state = {"sub": ctx.state.get("sub"), "shown": []}
    state["sub"] = picked(ctx, "subs", "open", state["sub"], ctx.state.get("shown") or [])
    d = run_kx(["subs", "-n", "100"])
    rows, sl = d["submissions"], d["slots"]
    shown = rows[: 40 if ctx.tight else 100]
    state["shown"] = [s["id"] for s in shown]
    sel = next((i for i, s in enumerate(shown) if s["id"] == state["sub"]), None)
    games = lambda s: (f"{s['games']['games']} games · {s['games']['winrate'] * 100:.1f}% won"  # noqa: E731
                       if (s.get("games") or {}).get("winrate") is not None else "")
    beside = bool(state["sub"]) and HOST.api >= 4
    cols = {"sub": 50, "run": 64, "status": 110, "public": 80, "private": 80, "when": 80, "games": 150, "message": 0}
    names = [k for k in cols if not beside or k in ("sub", "run", "status", "public", "message")]
    cells = lambda s: {"sub": s["id"], "run": s.get("run_id") or "external", "status": states.sub(s),  # noqa: E731
                       "public": num(s["public"], 4), "private": num(s["private"], 4), "when": f"{ago(s['created'])} ago",
                       "games": games(s), "message": s.get("error") or s.get("message") or ""}
    listing = table("subs", names, [[c[k] for k in names] for c in map(cells, shown)], widths=[cols[k] for k in names],
                    selectable=True, selected=sel, max_height=1200 if beside else 480) \
        if shown else label("subs-empty", "No submissions yet.")
    slots = f"{sl['used']} of {sl['limit']} today · resets in " if sl.get("limit") else ""
    from kx.views.text import dur
    head = header("Submissions", f"{slots}{dur(sl.get('reset_in_s')) if slots else ''}" + (f" · {sl['pending_on_kaggle']} pending on Kaggle"
                                                                                          if sl.get("pending_on_kaggle") else ""))
    card = sub_card(state["sub"]) if state["sub"] else []
    return page([head, text_input("open", "Open a submission", "", "S12 (Enter)"), listing, footer()], card), state


def sub_card(sub: str) -> list[dict]:
    from kx.ledger.store import parse_ts
    from kx.views.text import ago, num
    try:
        s = run_kx(["subs", sub])
    except KxError as e:
        return [row("card-head", md("card-title", f"**{esc(sub)}**: {esc(e)}"), button("close", "Close"))]
    kv = [("status", s["status"]), ("run", s.get("run_id") or "external"), ("public", num(s["public"], 5)),
          ("private", num(s["private"], 5) if s.get("private") is not None else None), ("message", s.get("message")),
          ("why", s.get("why")), ("expect", s.get("expect")), ("error", s.get("error")), ("target", s.get("target")),
          ("by", s.get("actor")), ("sent", f"{ago(s['created'])} ago")]
    out = [row("card-head", md("card-title", f"### {s['id']}\n\n" + "\n".join(f"- **{k}**: {esc(trunc(v, 400))}" for k, v in kv if v)),
               button("close", "Close"))]
    g = s.get("games") or {}
    if g.get("games"):
        out.append(md("card-games", f"**Games**: {g['games']} played · {g['win']} won · {g['loss']} lost · {g['tie']} tied"
                                    + (f" · {g['winrate'] * 100:.1f}% ±{g['se'] * 100:.1f} won" if g.get("winrate") is not None else "")))
        hist = [(parse_ts(h["ts"]).timestamp(), h["public"]) for h in s.get("history") or []
                if h.get("public") is not None and parse_ts(h.get("ts"))]
        if len(hist) >= 2:
            out.append(chart("card-rating", "rating over time", [{"name": "rating", "style": "line", "color": 0, "points": hist}],
                             x_label="date", y_label="rating", x_format="date", height=200))
        if g.get("opponents"):
            out.append(table("card-opponents", ["opponent", "won", "lost", "tied"],
                             [[o["name"], o["win"], o["loss"], o["tie"]] for o in g["opponents"][:60]], widths=[220, 60, 60, 60]))
    return out
