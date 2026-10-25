"""Commands: submit, subs, sync, kernel, dataset."""

from __future__ import annotations

import sys

from kx.cli.common import Out, S, argv, task_here
from kx.guard import actors, budgets
from kx.guard.gate import gate
from kx.ledger.config import KxError
from kx.views.text import ago, dim, dur, num, table


def cmd_submit(a):
    from kx.kaggle import submit
    with S() as st:
        res = submit.submit(st, actors.current(), a.run, a.message, why=a.why, expect=a.expect, yes=a.yes, argv=argv(),
                            file=a.file, kernel_version=a.kernel_version, early=a.early, task=task_here(st))
    return Out(res, f"submitted {res['submission']} ({res['run']})\n{res['output']}\nresults: kx sync, then kx subs")


def _ladder_lines(lad: dict) -> list[str]:
    lines = [f"ladder: the last {lad['active']} submissions play; each needs {lad['need']} games before it is retired"]
    lines += [f"  {s['id']} {s['run_id'] if s['run_id'] else 'external'}: {s['episodes'] or 0} games, public {num(s['public'])}"
              for s in lad["live"]]
    if lad["out"]:
        o = lad["out"]
        lines.append(("  a new submission retires " if lad["settled"] else "  not yet: a new submission would retire ")
                     + f"{o['id']} ({o['episodes'] or 0}/{lad['need']} games)")
    return lines


def cmd_subs(a):
    from kx.kaggle import ladder, submit
    if a.submission:
        return _one_sub(a.submission)
    with S() as st:
        subs = submit.listing(st, a.n)
        sl = {**submit.slots(st), "ladder": submit.ladder_slots(st)}
    rows = []
    for s in subs:
        g = s.get("games")
        games = (f"{g['win'] + g['loss'] + g['tie']} games, {g['winrate'] * 100:.1f}% won" if g and g.get("winrate") is not None
                 else (f"{s['episodes']} games" if s.get("episodes") else ""))
        rows.append([s['id'], s["run_id"] if s["run_id"] else "external", s["status"] or "", num(s["public"]),
                     ago(s["created"]) + " ago", s["actor"] or "", games, s["error"] or s["why"] or s["message"] or ""])
    foot = [f"{sl['used']}/{sl['limit']} today, resets in {dur(sl['reset_in_s'])}" if sl["limit"] else f"{sl['used']} today"]
    if sl["pending_on_kaggle"]:
        foot.append(f"{sl['pending_on_kaggle']} still pending on Kaggle (kx sync)")
    if sl["ladder"]:
        foot += _ladder_lines(sl["ladder"])
    text = table(rows, ["sub", "run", "status", "public", "when", "by", "games", "why / error"]) if rows else "no submissions"
    return Out({"submissions": subs, "slots": sl}, text + "\n" + dim("\n".join(foot)))


def _one_sub(ref) -> Out:
    """One submission: what it is and, in a simulation, its games (from the last kx sync)."""
    from kx.kaggle import ladder, submit
    with S() as st:
        s = submit.find(st, ref)
        summ = ladder.summary(st, s["id"], ladder.our_name(st))
        hist = ladder.history(st, s["id"])
    lines = [f"{s['id']} {s['status']}: {s['run_id'] if s['run_id'] else 'external'}, public {num(s['public'])}"
             + (f" · {s['message']}" if s.get("message") else "")]
    if summ:
        lines.append(f"{summ['games']} games, {summ['win']}W {summ['loss']}L {summ['tie']}T"
                     + (f" ({summ['winrate'] * 100:.1f}% ±{summ['se'] * 100:.1f})" if summ.get("winrate") is not None else ""))
        if summ.get("opponents"):
            lines.append(table([[o["name"], o["win"], o["loss"], o["tie"]] for o in summ["opponents"]], ["opponent", "W", "L", "T"], indent=2))
    if hist:
        lines.append(dim("rating: " + " → ".join(f"{num(h['public'])}@{h['episodes'] or 0}" for h in hist[-8:])))
    return Out({"submission": s['id'], **dict(s), "games": summ, "history": hist}, "\n".join(lines))


def cmd_sync(a):
    from kx.kaggle import submit
    from kx.ledger import sync
    with S() as st:
        try:  # the other machines' history first: their submissions are not "external" then
            led = sync.sync(st)
        except KxError as e:
            led = {"share": None, "error": str(e).splitlines()[0], "fix": e.fix}
            print(f"kx: ledger not synced: {led['error']}" + (f"\n  fix: {e.fix}" if e.fix else ""), file=sys.stderr)
        if a.ledger:
            return Out({"ledger": led}, sync.describe(led))
        r = submit.sync_submissions(st, games=not a.no_games)
        lb = None
        if not a.no_lb:
            try:
                lb = submit.sync_leaderboard(st)
            except KxError as e:
                print(f"kx: leaderboard not synced: {str(e).splitlines()[0]}", file=sys.stderr)
        if led.get("share"):  # what Kaggle said goes to the others now, not at the next sync
            try:
                sync.sync(st, up_only=True)
            except KxError as e:
                print(f"kx: ledger not synced: {str(e).splitlines()[0]}", file=sys.stderr)
    text = sync.describe(led) + f"\n{r['remote']} submission(s) on Kaggle: {r['new']} new, {r['updated']} updated"
    if lb:
        text += f"\nleaderboard: {lb['n_teams']} teams" + (f", we are #{lb['our_rank']} ({num(lb['our_score'])})" if lb["our_rank"] else "")
        medals = [f"{k} {num(lb[k])}" for k in ("gold", "silver", "bronze") if lb.get(k) is not None]
        if medals:
            text += f"\n  top {num(lb['top'])} · " + " · ".join(medals)
    return Out({**r, "leaderboard": lb, "ledger": led}, text)


def cmd_kernel(a):
    from kx.kaggle import kernels
    with S() as st:
        if a.action == "status":
            return Out(None, kernels.kernel_status(st, st.resolve(a.run) if a.run else None))
        who = actors.current()
        run_id = st.resolve(a.run)
        prev = kernels.push_kernel(st, run_id, dry_run=True)
        hours = float(st.comp.submission.get("kernel_gpu_hours", 1.0))
        cost = {"kaggle_gpu_hours": hours} if prev["gpu"] else {}
        budgets.check(st, who, cost)
        if cost:
            prev["kaggle_gpu_hours"] = f"{hours:g} (charged to the budget; submission.kernel_gpu_hours in comp.yaml)"
        gate(who, argv(), f"push kernel {prev['kernel']} for {run_id}", prev, yes=a.yes, task=task_here(st))
        out = kernels.push_kernel(st, run_id)
        if cost:
            budgets.charge(st, who, "kaggle_gpu_hours", hours, run_id)
    return Out({"output": out}, out)


def cmd_dataset(a):
    from kx.kaggle import kernels
    with S() as st:
        who = actors.current()
        run_id = st.resolve(a.run)
        prev = kernels.push_dataset(st, run_id, a.path, message=a.message or "", create=a.create, dry_run=True)
        gate(who, argv(), f"{prev['action']} of dataset {prev['dataset']} from {run_id}", prev, yes=a.yes, task=task_here(st))
        out = kernels.push_dataset(st, run_id, a.path, message=a.message or "", create=a.create)
    return Out({"output": out}, out)


def register(cmd, group, action):
    s = cmd("submit", cmd_submit, "build, check and preview a submission; --yes sends it")
    s.add_argument("run")
    s.add_argument("-m", "--message", required=True)
    s.add_argument("--why")
    s.add_argument("--expect")
    s.add_argument("--file", help="artifact file name (default: submission.file)")
    s.add_argument("--kernel-version")
    s.add_argument("--early", action="store_true", help="rl: retire a submission that has not played min_episodes games yet")
    s.add_argument("-y", "--yes", action="store_true", help="send it")
    s = cmd("subs", cmd_subs, "submissions with their results, today's slots (and the ladder); one: its games")
    s.add_argument("submission", nargs="?", help="S12: that submission and its games")
    s.add_argument("-n", type=int, default=15)
    s = cmd("sync", cmd_sync, "the ledger with the other machines (share: in machine.yaml), then submission results "
                              "(games, ratings) and a leaderboard snapshot from Kaggle")
    s.add_argument("--no-games", action="store_true")
    s.add_argument("--no-lb", action="store_true")
    s.add_argument("--ledger", action="store_true", help="only the ledger, not Kaggle")
    g = group("kernel", "code competitions: push a run's kernel, check its status")
    s = action(g, "push", cmd_kernel, "push the kernel as of the run's commit (preview; --yes sends)")
    s.add_argument("run")
    s.add_argument("-y", "--yes", action="store_true")
    s = action(g, "status", cmd_kernel, "status of the kernel (pushed for RUN)")
    s.add_argument("run", nargs="?")
    g = group("dataset", "upload a run's artifacts as a Kaggle dataset")
    s = action(g, "push", cmd_dataset, "upload a folder (with dataset-metadata.json) from the run (preview; --yes sends)")
    s.add_argument("run")
    s.add_argument("path")
    s.add_argument("-m", "--message")
    s.add_argument("--create", action="store_true")
    s.add_argument("-y", "--yes", action="store_true")
