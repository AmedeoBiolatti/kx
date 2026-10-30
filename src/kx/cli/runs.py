"""Commands: exec, runs, show, board, compare, search, note, tag, path, fetch, reap, progress, protocol."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from kx.board import findings, tasks
from kx.cli.common import Out, S
from kx.ledger import config
from kx.ledger.config import KxError
from kx.ledger.store import Store, loads, norm_tags
from kx.views.text import ago, bold, dim, dur, num, score, table, trunc, width


def _config_arg(s: str | None):
    if not s:
        return None
    if s.startswith("@"):
        s = Path(s[1:]).read_text()
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise KxError(f"--config must be JSON or @file.json ({e})") from None


def cmd_exec(a):
    from kx.runs.execute import run_command
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    from kx import sandbox
    if not a.on and sandbox.role() == "workspace":  # a workspace runs no project code: its runs go to an executor
        a.on = sandbox.machine().get("default_host")
        if not a.on:
            sandbox.no_local_runs("a run")
    if not a.on and not a.detached_to:
        from kx.guard import rules
        comp = config.load()
        a.on = rules.default_host(comp)  # the project's runs happen on its hosts (comp.yaml runs.on)
        if a.on:
            print(f"kx: this project's runs happen on {', '.join(rules.run_hosts(comp))} (comp.yaml runs.on): "
                  f"running on {a.on}", file=sys.stderr)
    if a.on:
        from kx.remote.execute import run_remote
        run_id, code = run_remote(cmd, a.on, hypothesis=a.hypothesis, kind="probe" if a.probe else "experiment",
                                  config=_config_arg(a.config), tags=a.tag or (), group=a.group, seed=a.seed,
                                  parent=a.parent, timeout=a.timeout, gpus=a.gpus or 0,
                                  detach=a.detach, echo=not a.json, needs=a.needs or (), task=a.task)
        return _ran(run_id, code, host=a.on, detached=a.detach)
    if a.detach:
        return _detach(a)
    started = None
    if a.detached_to:  # the background half of kx exec --detach
        import signal

        def stopped(*_):
            raise KeyboardInterrupt("stopped (SIGTERM)")
        signal.signal(signal.SIGTERM, stopped)

        def started(run_id):
            tmp = Path(a.detached_to + ".tmp")
            tmp.write_text(str(run_id))
            tmp.replace(a.detached_to)
    run_id, code = run_command(cmd, hypothesis=a.hypothesis, kind="probe" if a.probe else "experiment",
                               config=_config_arg(a.config), tags=a.tag or (), group=a.group, seed=a.seed,
                               parent=a.parent, timeout=a.timeout, gpus=a.gpus or 0,
                               echo=not a.json and not a.detached_to, started=started, task=a.task)
    return _ran(run_id, code)


def _ran(run_id: str, code: int, **extra) -> Out:
    """The end of kx exec: what the run came to (scores, rank, verdict against its parent or the best), so nobody
    has to ask with kx show and kx compare."""
    from kx.runs.compare import outcome, outcome_line
    with S() as st:
        o = None if extra.get("detached") else outcome(st, run_id)
    return Out({"run": run_id, "exit": code, **extra, "result": o},
               f"{run_id}: {outcome_line(o)}" if o else None, code)


DETACH_START_S = 120  # how long kx exec --detach waits for its background half to create the run


def _detach(a):
    """kx exec --detach on this machine: the same command line runs in a session of its own (it outlives this process
    and the terminal); this returns once its run exists, or with the error that kept it from starting."""
    import subprocess
    import tempfile
    import time
    from kx.cli import common
    n = len(common.ARGV) - len(a.cmd)
    head, tail = [x for x in common.ARGV[:n] if x not in ("--detach", "--json")], common.ARGV[n:]
    at = head.index("exec") + 1
    tmp = Path(tempfile.mkdtemp(prefix="kx-detach-"))
    started, log = tmp / "run", tmp / "log"
    child = [sys.executable, "-m", "kx", *head[:at], "--detached-to", str(started), *head[at:], *tail]
    with log.open("wb") as out:
        p = subprocess.Popen(child, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True)
    try:
        end = time.monotonic() + DETACH_START_S
        while not started.exists():
            if p.poll() is not None and not started.exists():
                lines = log.read_text(errors="replace").strip().splitlines()
                fix = next((x.strip()[5:] for x in lines if x.startswith("  fix: ")), None)
                msg = "\n".join(x.removeprefix("kx: ") for x in lines if not x.startswith("  fix: "))
                raise KxError(msg or f"the run did not start (exit {p.returncode})", fix=fix, code=p.returncode or 1)
            if time.monotonic() > end:
                raise KxError(f"the run has not started after {DETACH_START_S} s (process {p.pid})",
                              fix=f"kx runs --status running, or stop it: kill {p.pid}")
            time.sleep(0.1)
        run = started.read_text().strip()
    finally:
        for f in (started, log):
            f.unlink(missing_ok=True)
        tmp.rmdir()
    return Out({"run": run, "detached": True, "pid": p.pid},
               f"{run} started in the background (pid {p.pid})\n"
               f"  kx wait {run} [--until 'val_auc>=0.9'] · kx show {run} · stop it: kill {p.pid}")


def _ref_scores(st: Store) -> tuple[dict, dict]:
    from kx.runs.protocol import reference
    ref = reference(st)
    vals = {}
    if ref["id"]:
        vals = {r["run_id"]: r for r in st.all("SELECT run_id, value, se FROM scores WHERE protocol_id=?", (ref["id"],))}
    return ref, vals


def cmd_runs(a):
    from kx.views.status import remote_host
    with S() as st:
        where, params = [], []
        if a.task:
            where.append("task=?")
            params.append(st.resolve(a.task, "tasks"))
        if a.status:
            where.append("status=?")
            params.append(a.status)
        if a.tag:
            where.append("id IN (SELECT run_id FROM tags WHERE tag=?)")
            params.append(norm_tags([a.tag])[0])
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY n DESC, created DESC LIMIT ?"
        rows = st.all(sql, (*params, a.n))
        ref, vals = _ref_scores(st)
        notes = st.last_notes()
        latest = {}  # run -> its latest score under any protocol, for runs without a reference score
        bare = [r["id"] for r in rows if r["id"] not in vals]
        for i in range(0, len(bare), 500):  # under SQLite's limit on parameters
            chunk = bare[i:i + 500]
            for s in st.all(f"SELECT run_id, protocol_id, value, se FROM scores WHERE run_id IN ({','.join('?' * len(chunk))}) "
                            "AND protocol_id NOT LIKE 'kaggle:%' ORDER BY created", chunk):
                latest[s["run_id"]] = {"protocol_id": s["protocol_id"], "value": s["value"], "se": s["se"]}
        data, trows = [], []
        for r in rows:
            sc = vals.get(r["id"])
            other = None if sc else latest.get(r["id"])
            d = {"run": r["id"], "status": r["status"], "kind": r["kind"], "created": r["created"], "author": r["author"],
                 "task": r["task"], "hypothesis": r["hypothesis"], "note": notes.get(r["id"]),
                 "score": dict(sc) if sc else None, "other_score": dict(other) if other else None,
                 "host": remote_host(st, r), "finished": r["finished"]}
            if r["status"] == "running":
                from kx.runs.progress import snapshot
                p = (snapshot(st, r) or {}).get("progress") or {}
                d["progress"] = {k: p.get(k) for k in ("percent", "eta_s", "phase", "age_s")} if p.get("percent") is not None else None
            if a.curves:
                from kx.runs.progress import curve_names
                d["curves"] = curve_names(st.run_dir(r["id"]))
            if a.config:
                d.update(config=loads(r["config"]), metrics=loads(r["metrics"]), seed=r["seed"], group=r["grp"], n=r["n"],
                         git_sha=r["git_sha"], command=loads(r["command"]) if (r["command"] or "").startswith("[") else r["command"])
            data.append(d)
            shown = score(sc["value"], sc["se"]) if sc else (dim(f"{num(other['value'])} {other['protocol_id']}") if other else "")
            trows.append([d["run"], r["status"], ago(r["created"]), r["kind"][:5],
                          (r["author"] or "") + (f" @{d['host']}" if d["host"] else ""),
                          r['task'] if r["task"] else "", shown, r["hypothesis"] or ""])
    head = dim(f"score column: reference {ref['id']}" if ref["id"] else "no reference protocol yet")
    return Out(data, (table(trows, ["run", "status", "age", "kind", "by", "task", "score", "hypothesis"]) + "\n" + head)
               if trows else "no runs")


def cmd_show(a):
    from kx.runs.progress import artifacts as list_artifacts, curve_summary, sampled, snapshot
    with S() as st:
        r = st.get_run(st.resolve(a.run))
        d = dict(r)
        d["run"] = r["id"]
        for k in ("config", "env", "metrics"):
            d[k] = loads(d[k])
        d["command"] = loads(r["command"]) if r["command"] and r["command"].startswith("[") else r["command"]
        d["scores"] = [{"protocol": s["protocol_id"], "value": s["value"], "se": s["se"], "n": s["n"],
                        "breakdown": loads(s["breakdown"])} for s in st.scores_for(r["id"])]
        d["notes"] = [dict(n) for n in st.notes(r["id"])]
        d["tags"] = st.tags(r["id"])
        d["children"] = [c["id"] for c in st.all("SELECT id FROM runs WHERE parent=?", (r["id"],))]
        d["live"] = snapshot(st, r)
        d["curves"] = curve_summary(st.run_dir(r["id"]))
        if a.curves:  # cut to a few points per stretch of the run: its shape, extremes and restarts (progress.sampled)
            cut = sampled(st.run_dir(r["id"]), a.chunk, a.since)
            d["curve_points"], d["curve_times"] = cut.pop("points"), cut.pop("times")
            d["curve_sample"] = cut
        d["artifacts"], _ = list_artifacts(st.run_dir(r["id"]))
        from kx.runs.objects import objects
        d["objects"] = objects(st.run_dir(r["id"]), preview=True)  # r.log_table, by name
        d["dir"] = str(st.run_dir(r["id"]))
        from kx.kaggle.competition import push_url
        d["pushes"] = [{**dict(x), "url": push_url(x["kind"], x["target"])} for x in
                       st.all("SELECT kind, target, version, created, message FROM pushes WHERE run_id=? ORDER BY created, id",
                              (r["id"],))]
        from kx.remote.execute import info as remote_info
        d["remote"] = remote_info(st, r["id"])
    lines = [bold(f"{d['run']}  {d['status']}  {d['kind']}") + dim(f"  by {d['author']} · {ago(d['created'])} ago"
                                                                    + (f" · {d['task']}" if d["task"] else "")),
             f"  {d['hypothesis'] or ''}"]
    cmd = " ".join(map(shlex.quote, d["command"])) if isinstance(d["command"], list) else d["command"]
    lines.append(dim(f"  git {(d['git_sha'] or '-')[:10]}{'+dirty' if d['git_dirty'] else ''} on {d['git_branch'] or '-'}"
                     f" · {cmd}" + (f" · on {d['remote']['host']}" if d["remote"] else "")))
    if d["finished"]:
        from kx.ledger.store import parse_ts
        t0, t1 = parse_ts(d["created"]), parse_ts(d["finished"])
        lines.append(dim(f"  took {dur((t1 - t0).total_seconds()) if t0 and t1 else '-'}"))
    if d["status"] == "running":
        p = d["live"]["progress"]
        if p.get("total"):
            lines.append(f"  progress {p['completed']}/{p['total']} {p['unit']} ({p['percent']:.0f}%)"
                         + (f" eta {dur(p['eta_s'])}" if p.get("eta_s") else "") + f" · {p['phase']}")
    if d["scores"]:
        lines += ["", bold("scores")]
        lines.append(table([[s["protocol"], score(s["value"], s["se"]), f"n={s['n']}" if s["n"] else ""] for s in d["scores"]], indent=2))
    if d["config"]:
        lines += ["", bold("config"), "  " + trunc(json.dumps(d["config"]), 4 * width())]
    if d["metrics"]:
        lines += ["", bold("metrics"), "  " + ", ".join(f"{k}={num(v)}" for k, v in d["metrics"].items())]
    if d["curves"]:
        lines += ["", bold("curves")]
        lines.append(table([[k, f"{c['points']} pts", f"last {num(c['last'])} @ {num(c['last_step'])}",
                             f"min {num(c['min'])}", f"max {num(c['max'])}"] for k, c in d["curves"].items()], indent=2))
    for o in d["objects"]:
        pv = o.get("preview")
        if o["kind"] == "table" and pv:
            lines += ["", bold(f"table {o['name']}") + dim(f"  {o['metadata'].get('rows', len(pv['rows']))} rows")]
            lines.append(table([[num(c) if isinstance(c, float) else ("" if c is None else str(c)) for c in row] for row in pv["rows"][:30]],
                               pv["columns"], indent=2))
        elif o["kind"] == "image":
            lines += ["", bold(f"image {o['name']}") + dim(f"  {o['path']}")]
    if d["tags"]:
        lines += ["", bold("tags") + "  " + ", ".join(d["tags"])]
    if d["notes"]:
        lines += ["", bold("notes")] + [f"  {ago(n['ts'])} ago, {n['author']}: {n['text']}" for n in d["notes"]]
    if d["artifacts"]:
        lines += ["", bold("artifacts") + dim(f"  {d['dir']}")]
        lines.append(table([[x["path"], f"{x['bytes'] / 1e6:.1f} MB" if x["bytes"] > 1e5 else f"{x['bytes']} B"]
                            for x in d["artifacts"][:20]], indent=2))
    on_host = (d["remote"] or {}).get("on_host")
    if on_host:
        lines += ["", bold(f"still on {d['remote']['host']}") + dim(f"  kx fetch {d['run']} [PATH ...]")]
        lines.append(table([[x["path"], f"{x['bytes'] / 1e6:.0f} MB"] for x in on_host[:20]], indent=2))
    if d["parent"] or d["children"]:
        lines += ["", dim(f"parent {d['parent'] if d['parent'] else '-'} · children {', '.join(d['children']) or '-'}")]
    if d["error"]:
        lines += ["", bold("error"), "  " + d["error"][-1500:].replace("\n", "\n  ")]
    return Out(d, "\n".join(lines))


def _group_rows(rows):
    seen, out = {}, []
    for r in rows:
        g = r["grp"]
        if g and g in seen:
            seen[g]["more"] += 1
            continue
        d = {"row": r, "more": 0}
        if g:
            seen[g] = d
        out.append(d)
    return out


def cmd_board(a):
    from kx.runs.compare import compare_scores
    from kx.runs.protocol import ranking, reference
    with S() as st:
        if a.protocol:
            name = a.protocol
            row = st.one("SELECT id FROM protocols WHERE id=? OR name=? ORDER BY created DESC, rowid DESC LIMIT 1", (name, name))
            if row is None:
                raise KxError(f"no protocol {name!r}", fix="kx protocol list")
            pid, src = row["id"], "chosen"
        else:
            ref = reference(st)
            if not ref["id"]:
                raise KxError("no scored runs yet")
            pid, src = ref["id"], f"reference ({ref['source']})"
        rows = ranking(st, pid, include_discarded=a.all)
        p = st.protocol(pid)
        grouped = _group_rows(rows) if not a.all else [{"row": r, "more": 0} for r in rows]
        top = rows[0] if rows else None
        data, trows = [], []
        for i, g in enumerate(grouped[: a.n]):
            r = g["row"]
            vs = None
            if top is not None and r["id"] != top["id"]:
                c = compare_scores(dict(top), dict(r), p["direction"], st.comp.z, p["noise"])
                vs = {"z": c.z, "verdict": c.verdict}
            data.append({"rank": i + 1, "run": r["id"], "value": r["value"], "se": r["se"], "n": r["n"], "task": r["task"],
                         "group": r["grp"], "group_more": g["more"], "vs_best": vs, "hypothesis": r["hypothesis"]})
            vs_txt = "" if vs is None else (vs['verdict'] + (f" z={vs['z']:+.1f}" if vs["z"] is not None else ""))
            trows.append([i + 1, r["id"], score(r["value"], r["se"]), r["n"] or "", vs_txt,
                          (f"{r['grp']} (+{g['more']})" if g["more"] else (r["grp"] or "")),
                          r['task'] if r["task"] else "", r["hypothesis"] or ""])
    head = bold(pid) + dim(f"  {src} · {p['direction']} · {len(rows)} run(s)")
    return Out({"protocol": pid, "rows": data},
               head + "\n" + (table(trows, ["#", "run", "score", "n", "vs #1", "group", "task", "hypothesis"]) if trows else "  no runs"))


def cmd_compare(a):
    from kx.runs.compare import compare
    with S() as st:
        ra, rb = st.resolve(a.a), st.resolve(a.b)
        st.get_run(ra), st.get_run(rb)
        res = compare(st, ra, rb, st.comp.z)
    data = {"a": ra, "b": rb, "comparable": res.comparable,
            "comparisons": [c.__dict__ for c in res.comparisons], "only_a": res.only_a, "only_b": res.only_b, "hints": res.hints}
    lines = [f"is {rb} better than {ra}?"]
    if res.comparisons:
        lines.append(table([[c.protocol, num(c.a), num(c.b), f"{c.delta:+.4g}", f"z={c.z:+.2f}" if c.z is not None else "",
                             f"{c.symbol} {c.verdict}", c.method] for c in res.comparisons],
                           ["protocol", ra, rb, "Δ", "z", "verdict", "method"], indent=2))
    else:
        lines.append("  not comparable: no shared protocol")
    lines += [f"  hint: {h}" for h in res.hints]
    return Out(data, "\n".join(lines), 0 if res.comparable else 5)


def cmd_search(a):
    terms = [t.lower() for t in a.words]
    with S() as st:
        def match(text):
            t = (text or "").lower()
            return all(w in t for w in terms)
        runs = []
        notes: dict[int, list[str]] = {}
        for n in st.all("SELECT run_id, text FROM notes"):
            notes.setdefault(n["run_id"], []).append(n["text"])
        tagmap: dict[int, list[str]] = {}
        for t in st.all("SELECT run_id, tag FROM tags"):
            tagmap.setdefault(t["run_id"], []).append(t["tag"])
        for r in st.all("SELECT id, hypothesis, config, git_branch, status, created FROM runs ORDER BY n DESC, created DESC"):
            blob = " ".join([r["hypothesis"] or "", r["config"] or "", r["git_branch"] or "", *notes.get(r["id"], []),
                             *tagmap.get(r["id"], [])])
            if match(blob):
                runs.append({"run": r["id"], "status": r["status"], "hypothesis": r["hypothesis"],
                             "note": (notes.get(r["id"]) or [None])[-1]})
        fnd = [f for f in findings.listing(st, all_=True) if match(f["text"] + " " + " ".join(f["tags"]))]
        tsk = [t for t in tasks.listing(st, all_=True) if match(" ".join([t["title"], t["body"] or "", t["outcome"] or ""]))]
    data = {"runs": runs[: a.n], "findings": [{"id": f['id'], "status": f["status"], "text": f["text"]} for f in fnd[: a.n]],
            "tasks": [{"id": t['id'], "status": t["status"], "result": t["result"], "title": t["title"],
                       "outcome": t["outcome"]} for t in tsk[: a.n]]}
    lines = []
    if data["findings"]:
        lines += [bold("findings")] + [f"  {f['id']} [{f['status']}] {trunc(f['text'], width() - 20)}" for f in data["findings"]]
    if data["tasks"]:
        lines += [bold("tasks")] + [f"  {t['id']} [{t['status']}{'/' + t['result'] if t['result'] else ''}] {t['title']}"
                                    + (dim(f"\n      {trunc(t['outcome'], width() - 8)}") if t["outcome"] else "") for t in data["tasks"]]
    if data["runs"]:
        lines += [bold("runs")] + [f"  {r['run']} {r['status']:7} {trunc(r['hypothesis'], width() - 20)}"
                                   + (dim(f"\n      {trunc(r['note'], width() - 8)}") if r["note"] else "") for r in data["runs"]]
    return Out(data, "\n".join(lines) or "nothing found")


def cmd_note(a):
    with S() as st:
        run_id = st.resolve(a.run)
        if a.text:
            st.add_note(run_id, " ".join(a.text))
        notes = [dict(n) for n in st.notes(run_id)]
    return Out(notes, "\n".join(f"{ago(n['ts'])} ago, {n['author']}: {n['text']}" for n in notes) or "no notes")


def cmd_tag(a):
    with S() as st:
        run_id = st.resolve(a.run)
        st.get_run(run_id)
        if a.rm:
            st.remove_tags(run_id, a.tags)
        else:
            st.add_tags(run_id, a.tags)
            unknown = st.unknown_tags(a.tags)
            if unknown:
                print(f"kx: note: {', '.join(unknown)} not in comp.yaml tags ({', '.join(st.comp.tags)})", file=sys.stderr)
        t = st.tags(run_id)
    return Out({"run": run_id, "tags": t}, f"{run_id}: {', '.join(t) or 'no tags'}")


def cmd_fetch(a):
    from kx.remote.execute import fetch
    r = fetch(a.run, a.paths)
    if r.get("all_here"):
        return Out(r, f"{r['run']}: all of it is here already (its copy on {r['host']} was deleted: storage.keep: here)")
    left = r["on_host"]
    return Out(r, f"{r['run']}: copied {'everything' if r['fetched'] == 'all' else ', '.join(r['fetched'])} from {r['host']}"
                  + (f"; still there: {', '.join(x['path'] for x in left)}" if left else ""))


def cmd_gc(a):
    from kx.cli.common import argv
    from kx.guard import actors
    from kx.runs import gc
    with S() as st:
        p = gc.plan(st, a.runs, a.failed_days)
        kept = [f"  kept {k['run']}: {k['why']}" for k in p["kept"]]
        if not p["runs"]:
            return Out({**p, "freed": 0}, "\n".join(["nothing to delete", *kept]))
        rows = [[r["run"], f"{r['bytes'] / 2**30:.2f} GB", r["why"], f"and its copy on {r['on_host']}" if r.get("on_host") else ""]
                for r in p["runs"]]
        text = "\n".join([table(rows, ["run", "frees", "why", ""]), *kept,
                          f"{len(p['runs'])} run(s), {p['bytes'] / 2**30:.2f} GB here. What kx recorded stays (scores, "
                          "notes, curves, logs' end); what the runs made goes."])
        command = "kx " + " ".join(shlex.quote(x) for x in argv() if x not in ("-y", "--yes")) + " --yes"
        if actors.current().worker:
            raise KxError("workers do not delete run files", code=3, data={"preview": p, "command": command},
                          fix=f"leave it to your person: they run `{command}`")
        if not a.yes:
            return Out({**p, "command": command}, f"{text}\n  delete them: {command}", 3)
        res = gc.collect(st, p)
    return Out({**p, **res}, f"freed {res['freed'] / 2**30:.2f} GB from {len(res['runs'])} run(s)"
                             + "".join(f"\n  host copy kept: {e}" for e in res["host_errors"]))


def cmd_path(a):
    with S() as st:
        p = st.run_dir(st.resolve(a.run))
    return Out({"path": str(p)}, str(p))


def cmd_reap(a):
    with S() as st:
        closed = st.reap()
    return Out([{"run": i, "why": w} for i, w in closed],
               "\n".join(f"closed {i}: {w}" for i, w in closed) or "no dead runs")


def cmd_progress(a):
    from kx.runs.progress import write_progress
    d = os.environ.get("KX_RUN_DIR")
    if not d:
        raise KxError("kx progress only works inside kx exec (KX_RUN_DIR is not set)")
    metrics = {}
    for m in a.metric or []:
        k, _, v = m.partition("=")
        try:
            metrics[k] = float(v)
        except ValueError:
            raise KxError(f"--metric {m!r}: expected name=number") from None
    row = write_progress(Path(d), a.completed, a.total, unit=a.unit, phase=a.phase, values=metrics)
    return Out(row, None)


def cmd_protocol(a):
    from kx.runs.protocol import reference, superseded, use
    with S() as st:
        if a.action == "use":
            ref = use(st, a.name)
            return Out(ref, f"reference protocol: {ref['id']} (runs are ranked under it)")
        ref = reference(st)
        old = superseded(st)
        rows = st.all("SELECT p.*, COUNT(s.run_id) n, MAX(s.created) last FROM protocols p LEFT JOIN scores s ON s.protocol_id=p.id "
                      "GROUP BY p.id ORDER BY (p.name=?) DESC, last DESC", (ref["name"] or "",))
        data = [{"id": r["id"], "name": r["name"], "scores": r["n"], "last": r["last"], "superseded_by": old.get(r["id"]),
                 "reference": r["id"] == ref["id"], "spec": loads(r["spec"]), "noise": r["noise"]} for r in rows]
    trows = [[("* " if d["reference"] else "  ") + d["id"], d["scores"], ago(d["last"]) + " ago" if d["last"] else "",
              f"superseded by {d['superseded_by']}" if d["superseded_by"] else "", trunc(json.dumps(d["spec"]), 80)] for d in data]
    foot = dim(f"* reference ({ref['source']}). Change it: kx protocol use NAME") if ref["id"] else dim("no reference: kx protocol use NAME")
    return Out({"reference": ref, "protocols": data}, table(trows, ["protocol", "scores", "last", "", "spec"]) + "\n" + foot)


def register(cmd, group, action):
    s = cmd("exec", cmd_exec, "run any command as a tracked run")
    s.add_argument("-H", "--hypothesis")
    s.add_argument("--probe", action="store_true", help="allow uncommitted code (the patch is saved)")
    s.add_argument("--tag", action="append")
    s.add_argument("--group")
    s.add_argument("--task", help="the task it is for (default: your current one, kx task start)")
    s.add_argument("--seed", type=int)
    s.add_argument("--parent")
    s.add_argument("--config", help="JSON or @file.json")
    s.add_argument("--gpus", type=int, help="GPUs it uses: charged as gpu_hours (wall time × N)")
    s.add_argument("--timeout", type=float, help="seconds")
    s.add_argument("--on", metavar="HOST", help="run it on another machine (kx remote list)")
    s.add_argument("--detach", action="store_true", help="return once it started; it goes on in the background "
                   "(kx wait R; with --on: kx remote follow / sync)")
    s.add_argument("--detached-to", help=argparse.SUPPRESS)
    s.add_argument("--needs", action="append", metavar="PATH", help="with --on: a part of the data folder besides "
                   "Kaggle's files (e.g. derived/fp-v1), sent to the host if missing there (repeatable)")
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s = cmd("runs", cmd_runs, "recent runs, newest first")
    s.add_argument("-n", type=int, default=25)
    s.add_argument("--task")
    s.add_argument("--status", choices=("running", "done", "failed"))
    s.add_argument("--tag")
    s.add_argument("--curves", action="store_true", help="include the names of the curves each run logs (JSON)")
    s.add_argument("--config", action="store_true", help="include each run's config, reported metrics, seed and group (JSON)")
    s = cmd("show", cmd_show, "one run: scores, config, curves, notes, artifacts, error")
    s.add_argument("run")
    s.add_argument("--curves", action="store_true", help="include the curves (JSON), cut to their shape: the first, last, lowest "
                   "and highest point of each stretch of the run, and every restart")
    s.add_argument("--chunk", type=int, help=argparse.SUPPRESS)  # a live reader's last curve_sample: only what is new
    s.add_argument("--since", type=int, default=0, help=argparse.SUPPRESS)
    s = cmd("board", cmd_board, "runs ranked under the reference protocol (or another one)")
    s.add_argument("protocol", nargs="?")
    s.add_argument("-n", type=int, default=20)
    s.add_argument("--all", action="store_true", help="every run, discarded and grouped ones too")
    s = cmd("compare", cmd_compare, "is B better than A? (exit 5 when they share no protocol)")
    s.add_argument("a")
    s.add_argument("b")
    s = cmd("search", cmd_search, "runs, findings and tasks matching all the words")
    s.add_argument("words", nargs="+")
    s.add_argument("-n", type=int, default=15)
    s = cmd("note", cmd_note, "add a note to a run (no text: list its notes)")
    s.add_argument("run")
    s.add_argument("text", nargs="*")
    s = cmd("tag", cmd_tag, "tag a run")
    s.add_argument("run")
    s.add_argument("tags", nargs="+")
    s.add_argument("--rm", action="store_true")
    s = cmd("path", cmd_path, "a run's artifact directory")
    s.add_argument("run")
    s = cmd("fetch", cmd_fetch, "copy a remote run's big files (kept on its host) here: the paths given, or all")
    s.add_argument("run")
    s.add_argument("paths", nargs="*", help="relative to the run's dir, e.g. latest.pt")
    cmd("reap", cmd_reap, "close runs whose process died")
    s = cmd("gc", cmd_gc, "free the store: delete what discarded runs, failed ones and the runs named made (preview; --yes "
                          "deletes); what kx recorded stays")
    s.add_argument("runs", nargs="*", help="these runs (default: those tagged discarded, and failed ones)")
    s.add_argument("--failed-days", type=float, default=7, help="failed runs older than this many days (default 7)")
    s.add_argument("-y", "--yes", action="store_true", help="delete")
    s = cmd("progress", cmd_progress, "report progress from inside kx exec (non-Python commands)")
    s.add_argument("--completed", type=float, required=True)
    s.add_argument("--total", type=float)
    s.add_argument("--phase")
    s.add_argument("--unit")
    s.add_argument("--metric", action="append", help="name=value")

    g = group("protocol", "evaluation protocols and the reference one runs are ranked under")
    action(g, "list", cmd_protocol, "protocols with their scores")
    s = action(g, "use", cmd_protocol, "make NAME the reference protocol")
    s.add_argument("name")
