"""The Runs panel: the newest runs, filtered by status, tag and words, and one run opened (by its row, its id, or a
point on a chart elsewhere): scores, metrics, config, curves, notes and files, as `kx show`. With API 4 the run opened
is beside the list, which keeps its place."""

from __future__ import annotations

from kx.ledger.config import KxError
from kx.views.nextcode import Ctx, Go, footer, header, run_kx, states
from kx.views.nextcode.widgets import (HOST, button, chart, checkbox, col, esc, grid, label, md, row, select, sidebar,
                                       table, text_input, thin, tick, trunc)

STATUSES = ("all", "running", "done", "failed")
CARD = 640  # px: the run opened, beside the list (API 4)
# the list's columns (px, 0: the rest), and those it keeps beside a run opened
COLUMNS = {"run": 64, "status": 100, "when": 70, "took": 60, "by": 120, "task": 48, "score": 120, "hypothesis": 0}
BESIDE = ("run", "status", "score", "hypothesis")


def view(ctx: Ctx) -> tuple[dict, dict]:
    from kx.views.text import ago
    state = {"status": ctx.value("status", "all"), "tag": ctx.value("tag", "-"), "q": ctx.value("q", ""),
             "curves": ctx.value("curves", False), "run": ctx.state.get("run"), "shown": []}
    state["run"] = opened(ctx, state["run"], ctx.state.get("shown") or [])
    argv = ["runs", "-n", "300"] + (["--status", state["status"]] if state["status"] != "all" else []) \
        + (["--tag", state["tag"]] if state["tag"] != "-" else []) + (["--curves"] if state["curves"] else [])
    rows = run_kx(argv)
    words = state["q"].lower().split()
    hit = [r for r in rows if all(w in blob(r) for w in words) and (not state["curves"] or r.get("curves"))]
    shown = hit[: 30 if ctx.tight else 110]
    state["shown"] = [r["run"] for r in shown]
    filters = row("filters",
                  select("status", "Status", state["status"], [(s, s) for s in STATUSES]),
                  select("tag", "Tag", state["tag"], [("-", "any tag"), *((t, t) for t in tags())]),
                  text_input("q", "Words", state["q"], "in the id, hypothesis, note, host, task (Enter)"),
                  checkbox("curves", "only runs that log curves", state["curves"]),
                  text_input("open", "Open a run", "", "r0123 (Enter)"), gap=12)
    sel = next((i for i, r in enumerate(shown) if r["run"] == state["run"]), None)
    beside = bool(state["run"]) and HOST.api >= 4
    names = [k for k in COLUMNS if not beside or k in BESIDE]
    cells = lambda r: {"run": r["run"], "status": states.run(r), "when": f"{ago(r['created'])} ago", "took": took(r),  # noqa: E731
                       "by": (r.get("author") or "") + (f" @{r['host']}" if r.get("host") else ""),
                       "task": r.get("task") or "", "score": score_text(r), "hypothesis": r.get("hypothesis") or ""}
    listing = table("runs", names, [[c[k] for k in names] for c in map(cells, shown)], widths=[COLUMNS[k] for k in names],
                    selectable=True, selected=sel, max_height=1200 if beside else 560) \
        if shown else label("runs-empty", "No run matches." if rows else "No runs yet.")
    count = f"{len(shown)} of {len(hit)}" if len(shown) < len(hit) else f"{len(hit)}"
    head = header("Runs", f"{count} runs · newest first · score under the reference protocol"
                  + (" · click one to open it" if HOST.api >= 4 and not state["run"] else ""))
    state["_live"] = any(r["status"] == "running" for r in shown)
    card = run_card(state["run"], ctx.tight) if state["run"] else []
    if beside:  # the list keeps its place, the run beside it
        return sidebar("root", card, [head, filters, listing, footer()], width=CARD, right=True), state
    return col("root", head, filters, listing, col("card", *card) if card else None, footer(), gap=12), state


def opened(ctx: Ctx, current: str | None, shown: list[str]) -> str | None:
    """The run to show after this event: a row picked, an id typed, Close, or the one already open."""
    if ctx.clicked("close-run"):
        return None
    if ctx.clicked("train-run") and current:
        raise Go("training", runs=[current])
    if (picked := ctx.row("runs", shown)) is not None:
        return picked
    if (sent := ctx.args.get("open")) and isinstance(sent, str):
        return sent
    if typed := ctx.submitted("open"):
        try:
            return resolve(typed)
        except KxError:
            return typed  # its card says there is no such run
    return current


def resolve(ref: str) -> str:
    """r12, 12, r0012 or latest: the run's id (an error when there is none)."""
    from kx.ledger.store import Store
    from kx.views.nextcode import comp
    with Store(comp()) as st:
        return st.resolve(ref.strip())


def blob(r: dict) -> str:
    return " ".join(str(x or "") for x in (r["run"], r.get("hypothesis"), r.get("note"), r.get("host"), r.get("status"),
                                           r.get("task"), *(r.get("curves") or []))).lower()


def tags() -> list[str]:
    from kx.ledger.store import Store
    from kx.views.nextcode import comp
    with Store(comp()) as st:
        return [r[0] for r in st.all("SELECT tag, COUNT(*) n FROM tags GROUP BY tag ORDER BY n DESC LIMIT 31")]


def took(r: dict) -> str:
    from datetime import datetime, timezone

    from kx.ledger.store import parse_ts
    from kx.views.text import dur
    a, b = parse_ts(r.get("created")), parse_ts(r.get("finished")) or (datetime.now(timezone.utc) if r["status"] == "running" else None)
    return dur((b - a).total_seconds()) if a and b else ""


def score_text(r: dict) -> str:
    from kx.views.text import score
    if r.get("score"):
        return score(r["score"]["value"], r["score"]["se"])
    if r.get("other_score"):
        o = r["other_score"]
        return f"{score(o['value'], o['se'])} ({o['protocol_id'].split('@')[0]})"
    return ""


# --- one run


def run_card(ref: str | None, tight: bool) -> list[dict]:
    """A run as `kx show` has it, with Close; nothing when it is gone."""
    from kx.views.text import ago, num, score
    try:
        d = run_kx(["show", ref, "--curves"])
    except KxError as e:
        return [row("card-head", md("card-title", f"**{esc(ref)}**: {esc(e)}"), button("close-run", "Close"))]
    t = took(d)
    meta = [d["status"], f"{ago(d['created'])} ago", t and f"took {t}", d.get("author"),
            (d.get("remote") or {}).get("host") and f"@{d['remote']['host']}", d.get("task"),
            d.get("git_sha") and f"git {d['git_sha'][:10]}{' (dirty)' if d.get('git_dirty') else ''}",
            d.get("git_branch"), ", ".join(d.get("tags") or []) or None]
    command = d["command"] if isinstance(d["command"], str) else " ".join(d["command"] or [])
    head = md("card-title", f"### {d['run']} · {esc(d.get('hypothesis') or '')}\n\n" + " · ".join(esc(m) for m in meta if m)
              + (f"\n\n`{command[:300].replace('`', chr(39))}`" if command else "")
              + (f"\n\n**error**: {esc(trunc(d['error'], 600))}" if d.get("error") else ""))
    out = [row("card-head", head, button("train-run", "Show in Training"), button("close-run", "Close")), progress_of(d)]
    if d["scores"]:
        out.append(table("card-scores", ["protocol", "score", "n", "breakdown"],
                         [[s["protocol"], score(s["value"], s["se"]), s["n"] or "", breakdown(s.get("breakdown"))] for s in d["scores"]],
                         widths=[260, 140, 50, 0]))
    numbers = {k: v for k, v in (d.get("metrics") or {}).items() if not isinstance(v, (dict, list))}
    cfg = flatten(d.get("config") or {})
    pairs = [("metric", k, num(v) if isinstance(v, float) else v) for k, v in numbers.items()] \
        + [("config", k, v) for k, v in cfg.items()]
    if pairs:
        out.append(table("card-values", ["", "key", "value"], [[a, k, trunc(v, 200)] for a, k, v in pairs[: 25 if tight else 120]],
                         widths=[70, 260, 0]))
    cur = d.get("curve_points") or {}
    if cur:
        names = list(cur)[: 2 if tight else 4]
        out.append(md("card-curves-title", f"**Curves** · {len(cur)}" + (f", first {len(names)}: the Training panel has all"
                                                                           if len(names) < len(cur) else "")))
        out.append(grid("card-curves", *(chart(f"card-curve-{i}", k, [{"name": k, "color": 0, "points": thin([(p[0], p[1]) for p in cur[k]], 200)}],
                                               x_label="step", y_label="" if HOST.api >= 4 else k, height=160, legend=HOST.api < 4,
                                               sync="card-curves") for i, k in enumerate(names)), min_width=280))
    notes = d.get("notes") or []
    if notes:
        out.append(md("card-notes", "**Notes**\n\n" + "\n".join(f"- {esc(trunc(n['text'], 400))} · {ago(n['ts'])} ago"
                                                                 for n in notes[-(3 if tight else 10):])))
    files = d.get("artifacts") or []
    if files:
        out.append(md("card-files", f"**Files** · `{tick(d['dir'])}`\n\n" + "\n".join(
            f"- `{tick(f['path'])}` · {size_text(f.get('bytes'))}" for f in files[: 8 if tight else 30])))
    if d.get("children"):
        out.append(label("card-children", "children: " + ", ".join(d["children"][:40])))
    return [w for w in out if w]


def progress_of(d: dict):
    from kx.views.nextcode.widgets import progress
    from kx.views.text import dur
    p = ((d.get("live") or {}).get("progress") or {})
    if d["status"] != "running" or p.get("percent") is None:
        return None
    return progress("card-progress", f"{p['percent']:.0f}%" + (f" · eta {dur(p['eta_s'])}" if p.get("eta_s") else "")
                    + (f" · {p['phase']}" if p.get("phase") else ""), p["percent"] / 100)


def breakdown(b) -> str:
    from kx.views.text import num
    if not isinstance(b, dict):
        return ""
    return trunc(" · ".join(f"{k} {num(v, 3) if isinstance(v, float) else v}" for k, v in b.items()), 200)


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def size_text(n) -> str:
    if not isinstance(n, int):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""
