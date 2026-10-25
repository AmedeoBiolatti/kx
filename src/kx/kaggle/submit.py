"""Submissions: every check before anything is sent, a preview, then the upload; and syncing results back."""

from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kx.guard import budgets
from kx.guard.gate import gate
from kx.kaggle import artifacts, client as kaggle, competition, ladder
from kx.ledger.config import KxError
from kx.ledger.store import Store, norm_ts, now, parse_ts
from kx.runs.protocol import KAGGLE_PRIVATE, KAGGLE_PUBLIC, ensure_kaggle_protocols, reference

TAG = re.compile(r"\bkx:(r\d+(?:-[a-z]+-[a-z]+)?)\b")  # the run a submission came from, in its message
LADDER_FRESH_S = 600


def check_kernel_ref(kernel: str | None) -> str:
    if not kernel:
        raise KxError("comp.yaml has no submission.kernel", fix="set submission.kernel: <user>/<kernel-slug>")
    if "<" in kernel or kernel.count("/") != 1:
        raise KxError(f"comp.yaml submission.kernel must be <user>/<kernel-slug>, got {kernel!r}")
    return kernel


def kernel_version_for(store: Store, run_id: str, kernel: str, explicit=None) -> int:
    """The kernel version pushed for this run (`kx kernel push`)."""
    pushed = store.all("SELECT run_id, version FROM pushes WHERE kind='kernel' AND target=? AND version IS NOT NULL "
                       "ORDER BY created, id", (kernel,))
    mine = [p["version"] for p in pushed if p["run_id"] == run_id]
    if explicit is None:
        if not mine:
            raise KxError(f"no kernel version pushed for {run_id}", fix=f"kx kernel push {run_id}")
        return mine[-1]
    v = int(explicit)
    if v not in mine:
        owner = next((p["run_id"] for p in pushed if p["version"] == v), None)
        raise KxError(f"{kernel} version {v} " + (f"was pushed for {owner}" if owner else "was not pushed by kx")
                      + f", not for {run_id}", fix=f"kx kernel push {run_id}")
    return v


def submissions_today(store: Store) -> int:
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return store.one("SELECT COUNT(*) FROM submissions WHERE created >= ?", (midnight,))[0]


def slots(store: Store) -> dict:
    """Today's submission slots (Kaggle resets at 00:00 UTC)."""
    t = datetime.now(timezone.utc)
    reset = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    limit = store.comp.submission.get("daily_limit")
    used = submissions_today(store)
    return {"limit": limit, "used": used, "left": None if limit is None else max(0, limit - used),
            "reset_in_s": int((reset - t).total_seconds()),
            "pending_on_kaggle": store.one("SELECT COUNT(*) FROM submissions WHERE status IN "
                                           "('inflight','submitted','pending','running')")[0]}


def active_slots(comp) -> int | None:
    """rl: how many of the latest submissions keep playing (submission.active, default 2)."""
    n = comp.submission.get("active", 2 if comp.mode == "rl" else None)
    return int(n) if n else None


def count_episodes(rows: list[dict]) -> int:
    rows = [r for r in rows if ladder.is_ladder_game(r)]
    if rows and any("state" in r for r in rows):
        return sum(1 for r in rows if str(r.get("state", "")).upper().endswith("COMPLETED"))
    return len(rows)


def ladder_slots(store: Store, refresh: bool = False) -> dict | None:
    """rl: the submissions on the ladder (newest first) and the one a new submission would retire."""
    n = active_slots(store.comp)
    if not n:
        return None

    def rows():
        return store.all("SELECT * FROM submissions WHERE COALESCE(status,'') NOT IN ('error','unknown') "
                         "ORDER BY created DESC, n DESC LIMIT ?", (n,))
    live = rows()
    out = live[n - 1] if len(live) >= n else None
    need = store.comp.min_episodes
    if refresh and out is not None and out["ref"] and (out["episodes"] or 0) < need:
        at = parse_ts(out["episodes_at"])
        if at is None or (datetime.now(timezone.utc) - at).total_seconds() > LADDER_FRESH_S:
            try:
                eps = kaggle.episodes(out["ref"])
                store.update("submissions", {"episodes": count_episodes(eps), "episodes_at": now()}, id=out["id"])
                live = rows()
                out = live[n - 1]
            except KxError:
                pass
    return {"active": n, "need": need, "live": [dict(r) for r in live], "out": dict(out) if out else None,
            "settled": out is None or (out["episodes"] or 0) >= need}


def ladder_problem(store: Store, refresh: bool = True) -> str | None:
    lad = ladder_slots(store, refresh=refresh)
    if lad is None or lad["settled"]:
        return None
    o = lad["out"]
    return (f"only the last {lad['active']} submissions play: a new one would retire {o['id']} after "
            f"{o['episodes'] or 0}/{lad['need']} games, before its rating means anything")


def _best_line(store: Store, run_id: str) -> dict:
    ref = reference(store)
    if not ref["id"]:
        return {"reference": None}
    mine = store.one("SELECT value, se FROM scores WHERE run_id=? AND protocol_id=?", (run_id, ref["id"]))
    from kx.runs.protocol import ranking
    top = ranking(store, ref["id"], 1)
    from kx.views.text import score
    out = {"reference": ref["id"],
           "this_run": score(mine["value"], mine["se"]) if mine else "not scored under the reference protocol"}
    if top:
        out["best"] = f"{top[0]['id']} {score(top[0]['value'], top[0]['se'])}"
    return out


def plan(store: Store, run_id: str, message: str, *, file: str | None = None, kernel_version=None,
         early: bool = False, sync: bool = True) -> dict:
    """Every check that can be made before sending; builds and smoke-tests the artifact. Returns the preview."""
    comp = store.comp
    run = store.get_run(run_id)
    if run["status"] != "done":
        raise KxError(f"{run_id} is {run['status']}, not done", fix=f"kx show {run_id}")
    limit = comp.submission.get("daily_limit")
    if sync and limit is not None:
        try:
            sync_submissions(store, games=False)  # count submissions made outside kx too
        except KxError as e:
            print(f"kx: warning: could not sync submissions ({str(e).splitlines()[0]})", file=sys.stderr)
    sl = slots(store)
    if limit is not None and sl["left"] == 0:
        raise KxError(f"Kaggle's daily limit is reached ({sl['used']}/{limit})", code=4,
                      fix=f"wait for the 00:00 UTC reset (in {sl['reset_in_s'] // 3600}h{sl['reset_in_s'] % 3600 // 60:02d}m)")
    problems, notes = [], []
    if not early and (lp := ladder_problem(store)):
        problems.append(lp)
    sha7 = (run["git_sha"] or "nogit")[:7]
    full_msg = f"kx:{run_id} {sha7} {message}".strip()
    out_name = file or comp.submission.get("file", "submission.csv")
    preview = {"run": run_id, "hypothesis": run["hypothesis"], "git": sha7 + ("+dirty" if run["git_dirty"] else ""),
               "message": full_msg, "slots": f"{sl['used'] + 1}/{limit} today" if limit else "no daily limit known",
               **_best_line(store, run_id)}
    if comp.kind == "code":
        kernel = check_kernel_ref(comp.submission.get("kernel"))
        version = kernel_version_for(store, run_id, kernel, kernel_version)
        target = f"kernel:{kernel}@{version}"
        dup = store.one("SELECT id, ref FROM submissions WHERE target=? AND COALESCE(status,'') != 'error'", (target,))
        status, _ = kaggle.kernel_status(kernel, version)
        if status != "complete":
            problems.append(f"{kernel} version {version} is {status}, not complete (kx kernel status {run_id})")
        preview.update(target=target, output=out_name, kernel_status=status)
    else:
        art = artifacts.build(store, run_id, out_name, None)
        target = art["path"]
        dup = store.one("SELECT id, ref FROM submissions WHERE artifact=? AND COALESCE(status,'') != 'error'", (art["sha"],))
        if artifacts.smoke_enabled(comp):
            if art["smoke"] != "passed":
                ok, log = artifacts.smoke(store, art)
                if not ok:
                    raise KxError(f"smoke test failed on artifact {art['sha'][:12]}:\n{log[-1500:]}",
                                  fix="fix the submission, record a new run and submit that one")
            preview["smoke"] = "passed"
        else:
            notes.append("no submission.smoke in comp.yaml: the file was not smoke-tested")
        artifacts.verify(store, art)
        preview.update(target=target, artifact=art["sha"][:12], size=f"{(art['size'] or 0) / 2**20:.1f} MB")
    if dup:
        notes.append(f"this exact {'kernel version' if comp.kind == 'code' else 'file'} was already submitted as {dup['id']}")
    lad = ladder_slots(store)
    if lad and lad["out"]:
        o = lad["out"]
        preview["ladder"] = f"retires {o['id']} ({o['episodes'] or 0}/{lad['need']} games played)"
    if problems:
        raise KxError("cannot submit: " + "; ".join(problems),
                      fix="--early sends it anyway while the retired submission has few games" if any("retire" in p for p in problems) else None,
                      data={"preview": preview, "problems": problems})
    preview["notes"] = notes
    preview["_target"], preview["_kernel"] = target, (kernel, version) if comp.kind == "code" else None
    preview["_artifact"] = None if comp.kind == "code" else art["sha"]
    preview["_out_name"] = out_name
    return preview


def submit(store: Store, actor, run_ref, message: str, *, why: str | None = None, expect: str | None = None,
           yes: bool = False, argv: list[str] | None = None, file: str | None = None, kernel_version=None,
           early: bool = False, task: str | None = None) -> dict:
    run_id = store.resolve(run_ref)
    p = plan(store, run_id, message, file=file, kernel_version=kernel_version, early=early)
    notes = budgets.check(store, actor, {"submissions": 1})
    shown = {k: v for k, v in p.items() if not k.startswith("_")}
    shown.update(why=why, expect=expect)
    if notes:
        shown["notes"] = shown.get("notes", []) + notes
    gate(actor, argv or ["submit", run_id, "-m", message], f"submit {run_id} ({p.get('artifact') or p.get('target')})",
         shown, yes=yes, task=task)
    comp = store.comp
    with store.transaction():  # checked again and charged under the lock: two agents here cannot both pass a cap
        budgets.check(store, actor, {"submissions": 1})
        # one sent here, or a moment ago by another machine, that Kaggle may have (another machine's that stays
        # unknown is for it to settle)
        dupe = store.one(f"SELECT id FROM submissions WHERE status IN ('inflight','unknown') AND "
                         f"(writer IN ({','.join('?' * len(store.writers))}) OR created >= ?)",
                         (*store.writers, (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")))
        if dupe:
            raise KxError(f"{dupe['id']} was sent but kx does not know whether Kaggle got it", fix="kx sync")
        sid = store.new("submissions", {"run_id": run_id, "task": task, "created": now(), "actor": actor.name,
                                        "message": p["message"], "why": why, "expect": expect, "kind": comp.kind,
                                        "target": p["_target"], "artifact": p["_artifact"], "status": "inflight"})
        budgets.charge(store, actor, "submissions", 1, sid)  # Kaggle counts one it got, whatever kx hears back
    try:
        if comp.kind == "code":
            kernel, version = p["_kernel"]
            out = kaggle.submit_kernel(comp.slug, kernel, version, p["_out_name"], p["message"])
        else:
            out = kaggle.submit_file(comp.slug, comp.store / p["_target"], p["message"])
    except Exception:
        store.update("submissions", {"status": "unknown"}, id=sid)
        raise
    store.update("submissions", {"status": "submitted"}, id=sid)
    return {"submission": sid, "run": run_id, "output": out.strip(), "preview": shown}


def find(store: Store, ref) -> dict:
    """A submission by its id, number or name, or by Kaggle's id."""
    ref = str(ref).strip()
    try:
        key = store.resolve(ref, "submissions")
    except KxError:
        row = store.one("SELECT * FROM submissions WHERE ref=? ORDER BY created LIMIT 1", (ref,))
        if row is None:
            raise
        return dict(row)
    return dict(store.one("SELECT * FROM submissions WHERE id=?", (key,)))


def _status(s) -> str:
    return str(s or "").split(".")[-1].lower()


def failure_reason(ref: str) -> str | None:
    """Why a simulation submission errored: the traceback its agent printed in the validation episode."""
    try:
        eps = kaggle.episodes(ref)
    except KxError:
        return None
    ep = next((e for e in eps if "VALIDATION" in str(e.get("type", "")).upper()), eps[0] if eps else None)
    if not ep or ep.get("id") is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            kaggle.logs(str(ep["id"]), 0, Path(tmp))
        except KxError:
            return None
        for f in Path(tmp).glob("*.json"):
            try:
                steps = json.loads(f.read_text())
            except ValueError:
                continue
            errs = [x.get("stderr") for step in steps for x in (step if isinstance(step, list) else [step])
                    if isinstance(x, dict) and x.get("stderr")]
            if errs:
                lines = [ln.strip() for ln in errs[-1].splitlines() if ln.strip()]
                where = next((ln for ln in lines if "main.py" in ln and ln.startswith("File")), "")
                cause = next((ln for ln in lines if re.match(r"^\w+(Error|Exception)\b", ln)), lines[-1])
                return cause + (f" ({where})" if where else "") + f" in validation episode {ep['id']}"
    return None


def _explain_failures(store: Store, remote: list[dict]):
    desc = {str(s.get("ref")): s.get("errorDescription") for s in remote}
    for r in store.all("SELECT * FROM submissions WHERE status='error' AND error IS NULL AND ref IS NOT NULL"):
        reason = desc.get(str(r["ref"])) or (failure_reason(r["ref"]) if store.comp.kind == "simulation" else None) \
            or "Kaggle reported an error without a description"
        store.update("submissions", {"error": reason}, id=r["id"])


def sync_submissions(store: Store, remote: list[dict] | None = None, games: bool = True) -> dict:
    comp = store.comp
    ensure_kaggle_protocols(store)
    remote = kaggle.submissions(comp.slug) if remote is None else remote
    new = updated = 0
    for s in remote:
        ref = str(s.get("ref"))
        desc = s.get("description") or ""
        vals = {"status": _status(s.get("status")), "public": kaggle.to_float(s.get("publicScore")),
                "private": kaggle.to_float(s.get("privateScore"))}
        # the submission kx made first (another machine may have taken it as external before its log came)
        row = store.one("SELECT * FROM submissions WHERE ref=? AND kind IS NOT 'external'", (ref,)) or \
            store.one("SELECT * FROM submissions WHERE ref IS NULL AND message=? ORDER BY created, n LIMIT 1", (desc,)) or \
            store.one("SELECT * FROM submissions WHERE ref=?", (ref,))
        if row:  # only a change is written (and logged)
            if row["ref"] != ref or any(row[k] != v for k, v in vals.items()):
                store.update("submissions", {"ref": ref, **vals, "updated": now()}, id=row["id"])
                updated += 1
            continue
        m = TAG.search(desc)
        try:
            run_id = store.resolve(m.group(1)) if m else None
        except KxError:
            run_id = None
        store.new("submissions", {"ref": ref, "run_id": run_id, "created": norm_ts(s.get("date")), "message": desc,
                                  "kind": "external", "target": s.get("fileName"), **vals, "updated": now()})
        new += 1
    try:
        _explain_failures(store, remote)
    except Exception as e:  # noqa: BLE001 - explaining a failure must never break the sync
        print(f"kx: could not explain a failed submission: {e}", file=sys.stderr)
    _refresh_kaggle_scores(store)
    if comp.kind == "simulation" and games:
        refresh_episodes(store)
    for r in store.all("SELECT id, public, episodes FROM submissions WHERE public IS NOT NULL"):
        ladder.record_score(store, r["id"], r["public"], r["episodes"])
    store.set_setting("synced", now())
    return {"remote": len(remote), "new": new, "updated": updated}


def refresh_episodes(store: Store, limit: int = 6, log=None) -> int:
    """Games played by the latest submissions (the ones still on the ladder first)."""
    rows = store.all("SELECT * FROM submissions WHERE ref IS NOT NULL AND COALESCE(status,'') != 'error' "
                     "ORDER BY created DESC LIMIT ?", (limit,))
    n = 0
    for r in rows:
        try:
            eps = kaggle.episodes(r["ref"])
        except KxError as e:
            if log:
                log(f"could not fetch games of {r['id']}: {str(e).splitlines()[0]}")
            continue
        store.update("submissions", {"episodes": count_episodes(eps), "episodes_at": now()}, id=r["id"])
        ladder.store_episodes(store, r["id"], eps, r["ref"])
        n += 1
    return n


def _refresh_kaggle_scores(store: Store):
    """A run's leaderboard score is its latest scored submission's (n = how many were scored)."""
    for col, pid in (("public", KAGGLE_PUBLIC), ("private", KAGGLE_PRIVATE)):
        latest, count = {}, {}
        for r in store.all(f"SELECT run_id, {col} AS v FROM submissions WHERE run_id IS NOT NULL AND {col} IS NOT NULL"
                           " ORDER BY created, n"):
            latest[r["run_id"]] = r["v"]
            count[r["run_id"]] = count.get(r["run_id"], 0) + 1
        for run_id, v in latest.items():
            had = store.one("SELECT value, n FROM scores WHERE run_id=? AND protocol_id=?", (run_id, pid))
            if had is None or (had["value"], had["n"]) != (v, count[run_id]):
                store.insert("scores", {"run_id": run_id, "protocol_id": pid, "value": v, "se": None, "n": count[run_id],
                                        "breakdown": None, "created": now()}, on="replace")


def sync_leaderboard(store: Store, rows: list[dict] | None = None) -> dict:
    comp = store.comp
    rows = kaggle.leaderboard(comp.slug) if rows is None else rows
    rows = sorted((r for r in rows if int(r.get("Rank") or 0) >= 1), key=lambda r: int(r["Rank"]))
    n = len(rows)
    user = comp.username or kaggle.username()
    ours = next((r for r in rows if user and user.lower() in [u.strip() for u in (r.get("TeamMemberUserNames") or "").lower().split(",")]),
                None)
    category = ((competition.load_cached(comp) or {}).get("competition") or {}).get("category")
    medals = kaggle.awards_medals(category)
    th = kaggle.medal_thresholds(n) if n and medals else {}

    def score_at(rank):
        return kaggle.to_float(rows[min(rank, n) - 1].get("Score")) if n and rank >= 1 else None

    snap = {"ts": now(), "n_teams": n, "top": score_at(1), "our_rank": int(ours["Rank"]) if ours else None,
            "our_score": kaggle.to_float(ours.get("Score")) if ours else None, "gold": score_at(th.get("gold", 0)),
            "silver": score_at(th.get("silver", 0)), "bronze": score_at(th.get("bronze", 0))}
    store.insert("lb_snapshots", snap)
    if n and rows[0].get("LastSubmissionDate"):
        store.set_setting("lb_bounds", {"ts": snap["ts"], "days": lb_bounds(rows, medals)})
    return {**snap, "thresholds": th, "username": user, "medals": medals, "category": category}


def lb_bounds(rows: list[dict], medals: bool = True) -> list[dict]:
    """What one leaderboard download says about the medal lines on past days, for the days before kx first synced.
    A team that has not submitted since a day had its current score on that day; every other team had a score no better
    than now, or was not on the board yet. So the score at rank k at the end of a day lies between `worst`, the k-th best
    of the teams already final then (k at today's team count, the largest it can have been), and `best`, the k-th best
    score today (k at the count of teams already final, the smallest). `rows` in rank order; one entry per UTC day."""
    n, days = len(rows), sorted({r["LastSubmissionDate"][:10] for r in rows if r.get("LastSubmissionDate")})
    if not days:
        return []
    k_now = {"top": 1, **(kaggle.medal_thresholds(n) if medals else {})}
    out, d = [], datetime.fromisoformat(days[0]).date()
    while d.isoformat() <= days[-1]:
        final = [r for r in rows if (r.get("LastSubmissionDate") or "9")[:10] <= d.isoformat()]
        k_final = {"top": 1, **(kaggle.medal_thresholds(len(final)) if medals and final else {})}
        e = {"day": d.isoformat(), "final": len(final)}
        for key, k in k_now.items():
            worst = kaggle.to_float(final[k - 1].get("Score")) if len(final) >= k else None
            kb = k_final.get(key, 0) or 1
            e[key] = [worst, kaggle.to_float(rows[kb - 1].get("Score"))]
        out.append(e)
        d += timedelta(days=1)
    return out


def listing(store: Store, limit: int = 15) -> list[dict]:
    out = []
    for r in store.all("SELECT * FROM submissions ORDER BY created DESC, n DESC LIMIT ?", (limit,)):
        d = dict(r)
        if store.comp.kind == "simulation":
            d["games"] = ladder.summary(store, r["id"], ladder.our_name(store))
        out.append(d)
    return out
