"""The Overview panel's top: what changed since the person last looked (kx brief) and what needs them, settled with a
button as in the web dashboard. A decision's button runs the action kx lists for it (kx.views.actions): one with a
preview shows it first with Send, one that needs words opens a field. Chores run in one go; "Got it" starts the next
brief from now."""

from __future__ import annotations

import contextlib
import io
import re

from kx.ledger.config import KxError
from kx.ledger.store import now, age_s
from kx.views.nextcode import Ctx
from kx.views.nextcode.widgets import button, checkbox, col, esc, md, row, text_input, tick, trunc

FLASH_S = 120  # how long what an action came to stays on the panel


def key_of(x: dict) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{x['kind']}.{x.get('ref') or 'x'}")


def run(argv: list[str]) -> dict:
    """An action, in this process (its prints kept off stdout: that is the panel's response)."""
    from kx.views.actions import execute
    with contextlib.redirect_stdout(io.StringIO()):
        return execute(argv)


def flash(state: dict, text: str, bad: bool = False):
    state["flash"] = {"text": trunc(text, 600), "bad": bad, "at": now()}


def attempt(state: dict, argv: list[str]) -> dict | None:
    try:
        r = run(argv)
    except KxError as e:
        flash(state, str(e) + (f" (fix: {e.fix})" if e.fix else ""), bad=True)
        return None
    flash(state, r.get("text") or "done")
    return r


def handle(ctx: Ctx, state: dict, items: list[dict]) -> bool:
    """The person's clicks on this part of the panel; True when one changed the ledger (draw it afresh)."""
    state["deciding"] = ctx.state.get("deciding")
    state["flash"] = ctx.state.get("flash") if (age_s((ctx.state.get("flash") or {}).get("at")) or FLASH_S) < FLASH_S else None
    if ctx.clicked("seen"):
        return attempt(state, ["brief", "--mark"]) is not None
    if ctx.clicked("chores"):
        done = False
        for argv in dict.fromkeys(tuple(x["actions"][0]["argv"]) for x in items if x["who"] == "chore" and x["actions"]):
            done |= attempt(state, list(argv)) is not None
        return done
    if ctx.clicked("decide-cancel"):
        state["deciding"] = None
        return False
    byk = {key_of(x): x for x in items}
    if (hit := ctx.pressed("do-")) and "." in hit:
        k, j = hit.rsplit(".", 1)
        x = byk.get(k)
        if x is None or not j.isdigit() or int(j) >= len(x["actions"]):
            flash(state, "that is settled already", bad=True)
            return True
        a = x["actions"][int(j)]
        if a.get("input"):
            state["deciding"] = {"key": k, "j": int(j), "mode": "input"}
            return False
        if a.get("preview"):
            try:
                pv = run(a["preview"])
            except KxError as e:
                flash(state, str(e) + (f" (fix: {e.fix})" if e.fix else ""), bad=True)
                return False
            shown = {kk: trunc(" · ".join(map(str, v)) if isinstance(v, list) else str(v), 160)
                     for kk, v in (pv.get("preview") or {}).items()
                     if not kk.startswith("_") and v not in (None, "", []) and not (kk == "target" and str(v).startswith("/"))}
            state["deciding"] = {"key": k, "j": int(j), "mode": "confirm", "preview": dict(list(shown.items())[:16]),
                                 "problems": [trunc(p, 200) for p in pv.get("problems") or []][:5],
                                 "text": trunc(pv.get("text") or "", 400)}
            return False
        state["deciding"] = None
        return attempt(state, a["argv"]) is not None
    d = state["deciding"]
    text = ctx.submitted("decide-text") if d and d.get("mode") == "input" else None
    if d and (ctx.clicked("decide-ok") or text):
        x = byk.get(d["key"])
        if x is None or d["j"] >= len(x["actions"]):
            state["deciding"] = None
            flash(state, "that is settled already", bad=True)
            return True
        a = x["actions"][d["j"]]
        argv = list(a["argv"])
        if d["mode"] == "input":
            words = (text or ctx.inputs.get("decide-text") or "").strip()
            if not words:
                flash(state, "say something first: the agent that resumes the task reads it", bad=True)
                return False
            argv.append(words)
        state["deciding"] = None
        if attempt(state, argv) is not None and a.get("resume") and ctx.inputs.get("decide-resume", True) is not False:
            first = state["flash"]["text"]
            if attempt(state, a["resume"]) is not None:
                flash(state, first + "\n" + state["flash"]["text"])
        return True
    return False


def since(b: dict | None) -> list[dict]:
    if not b:
        return []
    from kx.views.text import ago, num
    c = b["changes"]
    lines = []
    r = c["runs"]
    if r["done"] or r["failed"]:
        best = c["best"]
        lines.append(f"- **runs** {r['done'] + len(r['failed'])} ended" + (f", {len(r['failed'])} failed" if r["failed"] else "")
                     + (f" · new best `{best['run']}` {num(best['value'])}" + (f" (was {num(best['was']['value'])}, `{best['was']['run']}`)"
                                                                             if best["was"] else "") if best else ""))
    for t in c["tasks"][:4]:
        lines.append(f"- **{t['id']}** {t['status']}" + (f", {t['result']}" if t["result"] else "") + f": {esc(trunc(t['outcome'] or t['title'], 140))}")
    if c["tasks_added"]:
        lines.append("- **new tasks** " + ", ".join(f"{t['id']} {esc(trunc(t['title'], 50))}" for t in c["tasks_added"][:5]))
    for f in c["findings"][:3]:
        lines.append(f"- **{f['id']}** {esc(trunc(f['text'], 140))}")
    subs = [f"{s['id']} scored {num(s['public'])}" if s["public"] is not None else f"{s['id']} sent"
            for s in c["submissions"] if s["public"] is not None or s["sent"]]
    if subs:
        lines.append("- **submissions** " + " · ".join(subs))
    if c["workers"]:
        lines.append("- **workers** " + " · ".join(f"{w['worker']} on {w['task']}" + (f" stopped: {esc(trunc(w['stop'], 50))}" if w["stop"]
                                                                                       else " ended") for w in c["workers"][:4]))
    if not lines:
        return [md("since", f"Nothing new since you last looked, {ago(b['since'])} ago.")]
    return [row("since-head", md("since-title", f"### Since you last looked · {ago(b['since'])} ago"), button("seen", "Got it"), gap=16),
            md("since", "\n".join(lines))]


def needs(items: list[dict], state: dict, ctx: Ctx) -> list[dict]:
    from kx.views.text import dur
    dec = [x for x in items if x["who"] == "decision"]
    chores = [x for x in items if x["who"] == "chore"]
    advice = [x for x in items if x["who"] == "advice"]
    out = []
    if f := state.get("flash"):
        out.append(md("flash", ("**✕** " if f["bad"] else "✓ ") + esc(f["text"]).replace("\n", "  \n")))
    if not items:
        return out + [md("needs", "✓ Nothing needs you right now.")]
    counts = [f"{len(dec)} decision{'s' if len(dec) != 1 else ''}"] + ([f"{len(chores)} chore{'s' if len(chores) != 1 else ''}"]
                                                                       if chores else []) + ([f"{len(advice)} to know"] if advice else [])
    out.append(md("needs-title", "### Needs you · " + " · ".join(counts)))
    d = state.get("deciding")
    for i, x in enumerate(dec[:6 if ctx.tight else 12]):
        k = key_of(x)
        what = f"**{esc(x['what'])}**" + (f" · {dur(x['age_s'])}" if x.get("age_s") is not None else "")
        if not x["actions"]:
            what += f"  `{tick(x['fix'])}`"
        out.append(row(f"need-{i}", md(f"need-{i}-what", what),
                       *[button(f"do-{k}.{j}", a["label"] + ("…" if a.get("input") or a.get("preview") else ""))
                         for j, a in enumerate(x["actions"])], gap=8))
        if d and d.get("key") == k and d["j"] < len(x["actions"]):
            out.append(opened(x["actions"][d["j"]], d, ctx))
    if len(dec) > (6 if ctx.tight else 12):
        out.append(md("needs-more", f"… {len(dec) - (6 if ctx.tight else 12)} more: `kx`"))
    if chores:
        cmds = list(dict.fromkeys("kx " + " ".join(x["actions"][0]["argv"]) for x in chores if x["actions"]))
        out.append(row("chores-row", md("chores-what", "**Chores** (bookkeeping anyone may do): "
                                        + "; ".join(esc(x["what"]) for x in chores[:4]) + f"  `{tick(' · '.join(cmds))}`"),
                       button("chores", "Do them") if cmds else None, gap=8))
    if advice:
        out.append(md("advice", "\n".join(f"- _{esc(x['what'])}_  `{tick(x['fix'])}`" for x in advice[:6])))
    return out


def opened(a: dict, d: dict, ctx: Ctx) -> dict:
    """A decision opened below its line: the preview with Send, or the field for the answer."""
    resume = checkbox("decide-resume", f"then resume it with {a['resume'][3]}", ctx.inputs.get("decide-resume", True) is not False) \
        if a.get("resume") else None
    actions = row("decide-actions", button("decide-ok", a["label"], disabled=bool(d.get("problems"))),
                  button("decide-cancel", "Cancel"), resume, gap=8)
    if d["mode"] == "input":
        return col("decide", text_input("decide-text", "", ctx.inputs.get("decide-text") or "", a["input"]), actions)
    lines = [f"- **{esc(k)}** {esc(v)}" for k, v in (d.get("preview") or {}).items()] or [esc(d.get("text") or "")]
    lines += [f"- **✕ {esc(p)}**" for p in d.get("problems") or []]
    return col("decide", md("decide-preview", "\n".join(lines)), actions)
