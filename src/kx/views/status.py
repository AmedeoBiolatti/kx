"""`kx status`: what is going on and what needs you. `kx context`: what an agent reads before it starts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from kx.board import findings, tasks
from kx.guard import budgets
from kx.kaggle import competition, ladder, submit
from kx.ledger.store import Store, age_s, loads, parse_ts, rid
from kx.runs.protocol import ranking, reference
from kx.views.text import ago, bold, dim, dur, num, score, table, trunc, width

NEEDS_SYNC_S = 12 * 3600


def cutoff(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _deadline(store: Store) -> dict:
    c = ((competition.load_cached(store.comp) or {}).get("competition") or {})
    d = parse_ts(c.get("deadline"))
    days = None if d is None else (d - datetime.now(timezone.utc)).total_seconds() / 86400
    return {"deadline": c.get("deadline"), "days_left": None if days is None else round(days, 1)}


def _run_line(store: Store, r) -> dict:
    from kx.runs.progress import snapshot
    snap = snapshot(store, r)
    p = snap["progress"]
    return {"run": rid(r["id"]), "task": r["task"], "author": r["author"], "hypothesis": r["hypothesis"],
            "elapsed_s": age_s(r["created"]), "heartbeat_age_s": age_s(r["heartbeat"]),
            "percent": p.get("percent"), "eta_s": p.get("eta_s"), "phase": p.get("phase") if p.get("total") else None,
            "host": remote_host(store, r)}


def remote_host(store: Store, r) -> str | None:
    """The host a run ran on with kx exec --on (None for a run on this machine)."""
    return r["host"] if r["host"] and (store.run_dir(r["id"]) / "remote.json").exists() else None


def needs_you(store: Store) -> list[dict]:
    out = []
    for r, why in store.stale_runs():
        out.append({"kind": "dead_run", "ref": rid(r["id"]), "age_s": age_s(r["created"]),
                    "what": f"{rid(r['id'])} is marked running but {why}", "fix": "kx reap"})
    from kx.remote import execute as remote
    for r, inf, a in remote.stale(store):
        out.append({"kind": "remote_run", "ref": rid(r["id"]), "age_s": age_s(r["created"]),
                    "what": f"{rid(r['id'])} runs on {inf['host']}; not copied back for {dur(a)} (nothing is recorded until then)",
                    "fix": f"kx remote sync  (or kx remote follow {rid(r['id'])})"})
    for s in store.all("SELECT * FROM submissions WHERE status IN ('inflight','unknown')"):
        out.append({"kind": "uncertain_submission", "ref": f"S{s['id']}", "age_s": age_s(s["created"]),
                    "what": f"S{s['id']} was sent but kx does not know whether Kaggle got it", "fix": "kx sync"})
    for s in store.all("SELECT * FROM submissions WHERE status='error' AND created >= ?", (cutoff(days=3),)):
        out.append({"kind": "failed_submission", "ref": f"S{s['id']}", "age_s": age_s(s["created"]),
                    "what": f"S{s['id']} ({rid(s['run_id']) if s['run_id'] else 'external'}) failed on Kaggle: "
                            f"{s['error'] or 'no reason given'}", "fix": "kx subs"})
    for t in tasks.listing(store):
        if t["status"] == "waiting" and (why := tasks.waiting_resolved(store, t)):
            out.append({"kind": "resume_task", "ref": f"T{t['id']}", "age_s": age_s(t["updated"]),
                        "what": f"T{t['id']} waited for {t['waiting_on']}: {why}, it can go on",
                        "fix": f"kx task start T{t['id']}  (or kx spawn T{t['id']} ...)"})
        elif t["status"] == "waiting" and ((on := t["waiting_on"] or "").startswith("kx ")
                                           or any(w in on.lower() for w in ("human", "you", "manual"))):
            out.append({"kind": "task_waits_on_you", "ref": f"T{t['id']}", "age_s": age_s(t["updated"]),
                        "what": f"T{t['id']} waits on you: {on}",  # a worker's `kx submit ... --yes`: run it yourself
                        "fix": on if on.startswith("kx ") else f"kx task show T{t['id']}"})
    for w in store.all("SELECT w.* FROM workers w LEFT JOIN tasks t ON t.id=w.task WHERE w.finished IS NOT NULL "
                       "AND w.finished >= ? AND COALESCE(w.stop, '') != 'asked' AND (w.code != 0 OR w.stop IS NOT NULL) "
                       "AND COALESCE(t.status, '') NOT IN ('done', 'dropped') "
                       "AND NOT EXISTS (SELECT 1 FROM workers w2 WHERE w2.task=w.task AND w2.started > w.started) "
                       "ORDER BY w.finished DESC LIMIT 5", (cutoff(days=1),)):
        out.append({"kind": "worker_failed", "ref": w["id"], "age_s": age_s(w["finished"]),
                    "what": f"worker {w['agent']} on T{w['task']} ended " + (f"(stopped: {w['stop']})" if w["stop"] else f"with exit {w['code']}"),
                    "fix": f"kx workers --show {w['id']}"})
    for t in store.all("SELECT * FROM tasks WHERE status='done' AND worktree IS NOT NULL ORDER BY updated DESC LIMIT 5"):
        out.append({"kind": "unmerged_task", "ref": f"T{t['id']}", "age_s": age_s(t["updated"]),
                    "what": f"T{t['id']} is done; its branch {t['branch']} is not merged", "fix": f"kx task merge T{t['id']}"})
    synced = store.setting("synced")
    open_subs = store.one("SELECT COUNT(*) FROM submissions WHERE status IN ('submitted','pending','running')")[0]
    if open_subs and (synced is None or (age_s(synced) or 0) > NEEDS_SYNC_S):
        out.append({"kind": "sync", "ref": None, "age_s": age_s(synced),
                    "what": f"{open_subs} submission(s) still pending on Kaggle; last sync {ago(synced) if synced else 'never'} ago",
                    "fix": "kx sync"})
    ref = reference(store)
    if ref["source"] == "inferred":
        out.append({"kind": "no_reference", "ref": None, "age_s": None,
                    "what": f"no reference protocol set: ranking under '{ref['name']}' (the most used lately)",
                    "fix": f"kx protocol use {ref['name']}  (kx protocol list)"})
    for b in budgets.report(store):
        if b["left"] <= 0:
            out.append({"kind": "budget", "ref": b["key"], "age_s": None,
                        "what": f"budget {b['scope']} {b['key']} is used up ({budgets.fmt(b['metric'], b['used'])}/"
                                f"{budgets.fmt(b['metric'], b['cap'])}); {b['frees_up']}", "fix": "kx budget"})
    return out


def results(store: Store, top: int = 3, recent: int = 3) -> dict:
    ref = reference(store)
    out = {"reference": ref, "best": [], "recent": [], "side": 0}
    if ref["id"]:
        for r in ranking(store, ref["id"], top):
            out["best"].append({"run": rid(r["id"]), "value": r["value"], "se": r["se"], "n": r["n"],
                                "hypothesis": r["hypothesis"], "task": r["task"], "created": r["created"]})
    seen = set()
    for r in store.all("SELECT r.id, r.hypothesis, r.created, r.task, s.protocol_id, s.value, s.se FROM runs r "
                       "JOIN scores s ON s.run_id=r.id WHERE r.status='done' AND s.protocol_id NOT LIKE 'kaggle:%' "
                       "ORDER BY r.id DESC, (s.protocol_id = ?) DESC LIMIT ?", (ref["id"] or "", recent * 3)):
        if r["id"] in seen or len(seen) >= recent:
            continue
        seen.add(r["id"])
        out["recent"].append({"run": rid(r["id"]), "protocol": r["protocol_id"], "value": r["value"], "se": r["se"],
                              "hypothesis": r["hypothesis"], "reference": r["protocol_id"] == ref["id"]})
    if ref["name"]:
        out["side"] = store.one("SELECT COUNT(DISTINCT name) FROM protocols WHERE name != ? AND id NOT LIKE 'kaggle:%'",
                                (ref["name"],))[0]
    return out


def submissions_summary(store: Store, n: int = 3) -> dict:
    subs = []
    us = ladder.our_name(store) if store.comp.kind == "simulation" else None
    for s in store.all("SELECT * FROM submissions ORDER BY created DESC, id DESC LIMIT ?", (n,)):
        d = {"id": f"S{s['id']}", "run": rid(s["run_id"]) if s["run_id"] else None, "status": s["status"],
             "public": s["public"], "created": s["created"], "episodes": s["episodes"], "error": s["error"]}
        if store.comp.kind == "simulation":
            g = ladder.summary(store, s["id"], us)
            if g and g.get("winrate") is not None:
                d["winrate"], d["decided"] = g["winrate"], g["win"] + g["loss"] + g["tie"]
        subs.append(d)
    lb = store.one("SELECT * FROM lb_snapshots ORDER BY ts DESC LIMIT 1")
    return {"recent": subs, "slots": submit.slots(store), "lb": dict(lb) if lb else None, "synced": store.setting("synced")}


def status(store: Store) -> dict:
    comp = store.comp
    running = [_run_line(store, r) for r in store.all("SELECT * FROM runs WHERE status='running' ORDER BY id")]
    from kx.workers.spawn import live
    workers = live(store)
    board = tasks.listing(store)
    counts = {s: sum(t["status"] == s for t in board) for s in ("active", "waiting", "open")}
    ready = [t for t in board if t["status"] == "open" and not t["blocked_by"]]
    return {
        "competition": {"slug": comp.slug, "kind": comp.kind, "mode": comp.mode, "metric": comp.metric,
                        "direction": comp.direction, **_deadline(store)},
        "needs_you": needs_you(store),
        "running": {"runs": running, "workers": workers},
        "tasks": {"counts": counts, "active": [t for t in board if t["status"] in ("active", "waiting")],
                  "ready": ready[:5]},
        "results": results(store),
        "submissions": submissions_summary(store),
        "budgets": budgets.report(store),
    }


def header(c: dict) -> str:
    left = f" · {c['days_left']:.0f} days left" if c.get("days_left") is not None and c["days_left"] >= 0 else \
        (" · ended" if c.get("days_left") is not None else "")
    name = c["slug"] if c.get("folder") in (None, c["slug"]) else f"{c['folder']} ({c['slug']})"
    return bold(name) + dim(f" · {c['kind']}/{c['mode']} · {c['metric']} ({c['direction']}){left}")


def render_status(st: dict, short: bool = False) -> str:
    W = width()
    out = [header(st["competition"]), ""]
    nyou = st["needs_you"]
    out.append(bold("NEEDS YOU") if nyou else bold("NEEDS YOU") + dim("  nothing"))
    for x in nyou:
        age = f" · {dur(x['age_s'])}" if x.get("age_s") is not None else ""
        out.append(f"  {trunc(x['what'], W - 6)}{dim(age)}")
        out.append(dim(f"      {x['fix']}"))
    if short:
        r = st["running"]
        c = st["tasks"]["counts"]
        out.append(dim(f"  {len(r['runs'])} run(s) and {len(r['workers'])} worker(s) running · tasks: "
                       f"{c['active']} active, {c['waiting']} waiting, {c['open']} open"))
        return "\n".join(out)
    out.append("")
    runs, workers = st["running"]["runs"], st["running"]["workers"]
    out.append(bold("RUNNING") + ("" if runs or workers else dim("  nothing")))
    rows = []
    for r in runs:
        prog = (f"{r['percent']:.0f}%" if r["percent"] is not None else "") + (f" eta {dur(r['eta_s'])}" if r["eta_s"] else "")
        who = r["author"] + (f" @{r['host']}" if r.get("host") else "")
        rows.append([r["run"], dur(r["elapsed_s"]), prog, f"T{r['task']}" if r["task"] else "", who, r["hypothesis"]])
    for w in workers:
        rows.append([w["id"][:8], dur(age_s(w["started"])), f"{(w['tokens_in'] or 0) + (w['tokens_out'] or 0):,} tok",
                     f"T{w['task']}" if w["task"] else "", f"worker {w['agent']}", w["last_action"] or ""])
    if rows:
        out.append(table(rows, indent=2))
    out.append("")
    c = st["tasks"]["counts"]
    out.append(bold("TASKS") + dim(f"  {c['active']} active · {c['waiting']} waiting · {c['open']} open"))
    trows = []
    for t in st["tasks"]["active"]:
        what = t["waiting_on"] if t["status"] == "waiting" else (t["last_note"]["text"] if t["last_note"] else "")
        trows.append([f"T{t['id']}", t["status"], t["owner"] or "", trunc(t["title"], 50), what])
    for t in st["tasks"]["ready"]:
        trows.append([f"T{t['id']}", "ready", f"p{t['priority']}", trunc(t["title"], 50), ""])
    if trows:
        out.append(table(trows, indent=2))
    out.append("")
    res = st["results"]
    ref = res["reference"]
    if ref["id"]:
        side = f" · {res['side']} side protocol(s)" if res["side"] else ""
        out.append(bold("RESULTS") + dim(f"  reference {ref['id']} ({ref['source']}){side}"))
        rows = [[f"#{i + 1}", b["run"], score(b["value"], b["se"]), f"T{b['task']}" if b["task"] else "", b["hypothesis"]]
                for i, b in enumerate(res["best"])]
        rows += [["new", x["run"], score(x["value"], x["se"]), "" if x["reference"] else x["protocol"], x["hypothesis"]]
                 for x in res["recent"] if x["run"] not in {b["run"] for b in res["best"]}]
        if rows:
            out.append(table(rows, indent=2))
    else:
        out.append(bold("RESULTS") + dim("  no scored runs yet"))
    out.append("")
    sub = st["submissions"]
    sl = sub["slots"]
    lb = sub["lb"]
    head = f"  {sl['used']}/{sl['limit']} today" if sl["limit"] else ""
    if lb and lb.get("our_rank"):
        head += f" · rank {lb['our_rank']}/{lb['n_teams']} ({ago(lb['ts'])} ago)"
    out.append(bold("SUBMISSIONS") + dim(head))
    rows = []
    for s in sub["recent"]:
        extra = ""
        if s.get("winrate") is not None:
            extra = f"{s['decided']} games, {s['winrate'] * 100:.1f}% won"
        elif s.get("error"):
            extra = s["error"]
        rows.append([s["id"], s["run"] or "external", s["status"] or "", num(s["public"]), ago(s["created"]) + " ago", extra])
    if rows:
        out.append(table(rows, indent=2))
    b = [x for x in st["budgets"] if x["cap"]]
    if b:
        out.append("")
        out.append(bold("BUDGETS"))
        out.append(table([[x["scope"], x["key"], f"{budgets.fmt(x['metric'], x['used'])} / {budgets.fmt(x['metric'], x['cap'])}",
                           x["frees_up"]] for x in b], indent=2))
    return "\n".join(out).rstrip() + "\n"


# -- context: what an agent reads first ------------------------------------------------------------------------------

RULES = [
    "Record every experiment with kx: `kx exec -H \"hypothesis\" -- cmd` or `with kx.run(...) as r:` in Python. "
    "Commit first: experiments refuse uncommitted code (`--probe` / kind='probe' saves the patch instead).",
    "Score under the reference protocol so runs are ranked; a new protocol name is only a side evaluation.",
    "`kx search WORDS` before trying an idea; record conclusions as findings (`kx finding add TEXT --evidence rN --tag T`), "
    "negative ones too.",
    "Submissions and kernel/dataset pushes show a preview and need --yes. Ask the person you work for before passing it; "
    "a worker's request goes to them instead (exit 3).",
    "Exit codes: 0 ok · 1 error (read the fix: line) · 3 needs confirmation · 4 over a budget or limit · 5 not comparable. "
    "Every command takes --json.",
]


def context(store: Store, actor, task_id: int | None = None) -> dict:
    st = status(store)
    t = None
    if task_id:
        try:
            t = tasks.get(store, task_id)
            t["notes"] = tasks.notes(store, task_id, limit=8)
            t["blocked_by"] = tasks.blockers(store, t)
        except Exception:  # noqa: BLE001 - a bad KX_TASK must not break the context
            t = None
    ref = st["results"]["reference"]
    how = None
    if ref["id"]:
        row = store.one("SELECT r.command FROM runs r JOIN scores s ON s.run_id=r.id WHERE s.protocol_id=? "
                        "ORDER BY r.id DESC LIMIT 1", (ref["id"],))
        if row and row["command"]:
            try:
                how = " ".join(json.loads(row["command"]))
            except (ValueError, TypeError):
                how = row["command"]
        spec = store.one("SELECT spec, noise FROM protocols WHERE id=?", (ref["id"],))
        ref = {**ref, "spec": loads(spec["spec"]) if spec else None, "scored_by": how}
    return {
        "you": {"name": actor.name, "worker": actor.worker},
        "competition": st["competition"],
        "scoring": list(store.comp.scoring.guidance[:3]),
        "task": t,
        "reference": ref,
        "best": st["results"]["best"],
        "recent": st["results"]["recent"],
        "submissions": st["submissions"]["recent"],
        "slots": st["submissions"]["slots"],
        "tasks": [{"id": f"T{x['id']}", "status": x["status"], "title": x["title"], "owner": x["owner"]}
                  for x in tasks.listing(store)][:10],
        "findings": [{"id": f"F{f['id']}", "status": f["status"], "tags": f["tags"], "text": f["text"]}
                     for f in findings.listing(store, limit=8)],
        "needs_you": st["needs_you"] if not actor.worker else [],
        "rules": RULES,
    }


def render_context(cx: dict) -> str:
    c = cx["competition"]
    out = [f"# kx context: {c['slug']}", ""]
    who = f"worker {cx['you']['name']}" if cx["you"]["worker"] else f"{cx['you']['name']} (or the agent they work with)"
    left = f", {c['days_left']:.0f} days left" if c.get("days_left") is not None and c["days_left"] >= 0 else ""
    out.append(f"You: {who}. Competition: {c['kind']} submissions, mode {c['mode']}, metric {c['metric']} ({c['direction']}){left}.")
    out += [f"- {g}" for g in cx["scoring"]]
    t = cx["task"]
    if t:
        out += ["", f"## Your task: T{t['id']} {t['title']} ({t['status']})"]
        if t.get("body"):
            out.append(trunc(t["body"], 1200))
        if t.get("blocked_by"):
            out.append(f"Depends on, not done yet: {', '.join(t['blocked_by'])}")
        for n in t.get("notes") or []:
            out.append(f"- {ago(n['ts'])} ago, {n['author']}: {trunc(n['text'], 200)}")
        out.append(f"Progress: `kx task note T{t['id']} \"...\"`. Finish: `kx task done T{t['id']} --result "
                   f"positive|negative|inconclusive --outcome \"what was learned\"`, or `kx task wait T{t['id']} --on WHAT`.")
    ref = cx["reference"]
    out += ["", "## Results"]
    if ref.get("id"):
        out.append(f"Reference protocol {ref['id']} ({ref['source']}). Spec: {trunc(json.dumps(ref.get('spec')), 300)}")
        if ref.get("scored_by"):
            out.append(f"Last scored by: {trunc(ref['scored_by'], 200)}")
        for i, b in enumerate(cx["best"]):
            out.append(f"{i + 1}. {b['run']} {score(b['value'], b['se'])}  {trunc(b['hypothesis'], 110)}")
    else:
        out.append("No scored runs yet: define the evaluation protocol first (kx.Protocol in the eval code).")
    for x in cx["recent"]:
        if x["run"] not in {b["run"] for b in cx["best"]}:
            out.append(f"recent: {x['run']} {score(x['value'], x['se'])} under {x['protocol']}  {trunc(x['hypothesis'], 90)}")
    if cx["submissions"]:
        sl = cx["slots"]
        out += ["", "## Submissions" + (f" ({sl['used']}/{sl['limit']} today)" if sl["limit"] else "")]
        for s in cx["submissions"]:
            extra = f", {s['decided']} games {s['winrate'] * 100:.1f}% won" if s.get("winrate") is not None else ""
            out.append(f"- {s['id']} {s['run'] or 'external'}: {s['status']}, public {num(s['public'])}{extra}, {ago(s['created'])} ago")
    if cx["tasks"]:
        out += ["", "## Open tasks"]
        out += [f"- {x['id']} [{x['status']}{', ' + x['owner'] if x['owner'] else ''}] {trunc(x['title'], 100)}" for x in cx["tasks"]]
    if cx["findings"]:
        out += ["", "## Recent findings (kx finding show F<n> for the full text)"]
        out += [f"- {f['id']} [{f['status']}{'; ' + ','.join(f['tags']) if f['tags'] else ''}] {trunc(f['text'], 180)}"
                for f in cx["findings"]]
    if cx["needs_you"]:
        out += ["", "## Waiting on the person driving"]
        out += [f"- {trunc(x['what'], 160)}  ({x['fix']})" for x in cx["needs_you"]]
    out += ["", "## Rules"] + [f"- {r}" for r in cx["rules"]]
    return "\n".join(out).rstrip() + "\n"
