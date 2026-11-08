"""kx's states as table cells: an icon in the state's color before a word (NextCode 0.1.6 draws the icons; elsewhere
the word, or the icon's label when there is none, is the cell's text)."""

from __future__ import annotations

from kx.views.nextcode.widgets import AMBER, BLUE, GREEN, GREY, RED, cell, mark


def run(r: dict, words: bool = True) -> tuple:
    """A run's status (running with its progress). words=False: the icon alone where it is drawn."""
    s = r["status"]
    p = r.get("progress") or ((r.get("live") or {}).get("progress")) or {}
    if s == "running":
        pct = f"{p['percent']:.0f}%" if p.get("percent") is not None else ("running" if words else "")
        return cell(pct, mark("clock", "running", BLUE))
    icon = {"done": mark("check", "done", GREEN), "failed": mark("cross", "failed", RED)}.get(s) or mark("circle", s, GREY)
    return cell(s if words else "", icon)


def task(t: dict) -> tuple:
    """A task's state: ready or blocked (open), active, waiting, done (with its result) or dropped."""
    s = t["status"]
    if s == "open":
        return cell("blocked", mark("warning", "blocked", AMBER)) if t.get("blocked_by") else cell("ready", mark("circle", "ready"))
    text = s + (f" · {t['result']}" if t.get("result") else "")
    icon = {"active": mark("dot", "active", BLUE), "waiting": mark("clock", "waiting", AMBER),
            "done": mark("check", "done", GREEN), "dropped": mark("minus", "dropped", GREY)}.get(s)
    return cell(text, icon)


def sub(s: dict) -> tuple:
    """A submission's status: complete, pending on Kaggle (submitted, pending, inflight), error or unknown."""
    st = s["status"] or "unknown"
    icon = {"complete": mark("check", "complete", GREEN), "error": mark("cross", "error", RED),
            "unknown": mark("circle", "unknown", GREY)}.get(st) or mark("clock", st, BLUE)
    return cell(st, icon)


VERDICTS = {"better": ("check", GREEN), "worse": ("cross", RED), "within noise": ("dot", None),
            "inconsistent": ("warning", AMBER)}  # minus, beside a number, would read as its sign


def verdict(text: str, v: dict | None, base: str) -> tuple:
    """A score with `kx compare`'s verdict on it against the base run: an icon, the verdict on hover. Where icons
    aren't drawn, the verdict's symbol and word follow the score."""
    from kx.views.nextcode.widgets import HOST
    if not v:
        return cell(text)
    if not HOST.marks:
        from kx.runs.compare import Comparison
        return cell(f"{text}  {Comparison(**v).symbol} {v['verdict']}")
    icon, color = VERDICTS.get(v["verdict"], ("circle", GREY))
    return cell(text, mark(icon, f"{v['verdict']} than {base}" if v["verdict"] in ("better", "worse")
                           else f"{v['verdict']} against {base}", color))
