"""`kx nextcode panel`: kx's NextCode panel, drawn whole here.

The extension (nextcode/ in the kx repo) starts this command for every invocation of its panel, and NextCode takes
what it prints as the extension's response. The panel has the web dashboard's pages as tabs: overview (kx status,
score and leaderboard charts), runs, training (a workspace of runs' curves, as wandb's), tasks, findings and
submissions, from the same data as the CLI's --json. Its state is the page open and each page's own (filters, the
runs shown, the run opened), which comes back with every invocation; the form's current values (inputs) win over it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from kx.ledger import config
from kx.ledger.config import KxError
from kx.views.nextcode.widgets import HOST, button, col, esc, is_sidebar, md, problems, row, tabs, tick

MAX_BYTES: int | None = None  # the most kx prints: the host's (NextCode keeps 256 KiB of a process's output, 1 MiB with API 4)
PAGES = {"overview": "Overview", "runs": "Runs", "training": "Training", "tasks": "Tasks", "findings": "Findings",
         "subs": "Submissions"}
LIVE_MS = 10_000


def refresh_ms() -> int:
    """How often a page is drawn again when nothing it shows runs (while something runs: LIVE_MS; a page says so with
    state["_live"]). Seldom with API 3, because NextCode shows a "Running…" line above the panel while kx draws it,
    which moves the panel down; API 4 hosts show that in a corner instead."""
    return 30_000 if HOST.api >= 4 else 60_000


def max_bytes() -> int:
    return MAX_BYTES or HOST.limits["output"]


def state_bytes() -> int:
    return HOST.limits["state"]


class Go(Exception):
    """A page sends the person to another page, with args for it (Show in Training: the runs; a row of the
    Overview's tables: the task or submission to open)."""

    def __init__(self, page: str, **args):
        super().__init__(page)
        self.page, self.with_ = page, args  # not .args: Exception's, always a tuple


class Ctx:
    """One invocation of a panel: what it showed (state), the form (inputs) and what was done (event)."""

    def __init__(self, inv: dict, tight: bool):
        self.state = inv.get("state") if isinstance(inv.get("state"), dict) else {}
        self.inputs = inv.get("inputs") if isinstance(inv.get("inputs"), dict) else {}
        ev = inv.get("event") or {}
        self.widget = ev.get("widget") if inv.get("kind") == "ui-event" else None
        self.kind = (ev.get("kind") or {}).get("kind")
        self.event = ev.get("kind") or {}
        self.tight = tight  # the first try did not fit: show less
        self.args = inv.get("args") if isinstance(inv.get("args"), dict) else {}

    def value(self, key: str, default=None):
        """The form's value of a control, else what the panel had, else the default."""
        v = self.inputs.get(key, self.state.get(key, default))
        return v if isinstance(v, type(default)) or default is None else default

    def clicked(self, widget: str) -> bool:
        return self.widget == widget and self.kind in ("click", "submit")

    def submitted(self, widget: str) -> str | None:
        """The text of a text input the person pressed Enter in."""
        if self.widget == widget and self.kind == "submit":
            v = self.inputs.get(widget)
            return v.strip() if isinstance(v, str) and v.strip() else None
        return None

    def selected(self, chart: str) -> tuple[int, int] | None:
        """(series, index) of a chart point clicked."""
        if self.widget == chart and self.kind == "chart-select":
            return self.event.get("series"), self.event.get("index")
        return None

    def pressed(self, prefix: str) -> str | None:
        """The rest of a clicked button's id that starts with prefix (remove-r0012 -> r0012)."""
        if self.kind == "click" and self.widget and self.widget.startswith(prefix):
            return self.widget[len(prefix):]
        return None

    def changed(self, widget: str):
        """The new value of a select or checkbox changed."""
        return self.event.get("value") if self.widget == widget and self.kind == "change" else None

    def row(self, table: str, keys: list):
        """The key of a table's row clicked (`select-row`, API 4); keys: its rows, as shown."""
        if self.widget == table and self.kind == "select-row" and 0 <= (i := self.event.get("index", -1)) < len(keys):
            return keys[i]
        return None

    def toggled(self, checklist: str, keys: list[str]) -> tuple[str, bool] | None:
        """(key, ticked) of a row of a checklist ticked or unticked; keys: its rows, as shown."""
        if self.widget == checklist and self.kind == "check-row" and 0 <= (i := self.event.get("index", -1)) < len(keys):
            return keys[i], bool(self.event.get("value"))
        if self.kind == "change" and self.widget and self.widget.startswith(f"{checklist}-"):
            return self.widget[len(checklist) + 1:], bool(self.event.get("value"))
        return None


def views():
    from kx.views.nextcode import lists, overview, runs, training
    return {"overview": overview.view, "runs": runs.view, "training": training.view, "tasks": lists.tasks,
            "findings": lists.findings, "subs": lists.subs}


def response(invocation: dict | None = None) -> dict:
    """The response to one invocation of the panel (open-panel, ui-event, refresh or process-exit): the page open,
    or the one its tab, or a page's link, goes to."""
    inv = invocation or {}
    panel = inv.get("id") if inv.get("kind") == "open-panel" else inv.get("panel")
    panel = panel if isinstance(panel, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", panel) else "kx"
    st = inv.get("state") if isinstance(inv.get("state"), dict) else {}
    pages = {k: v for k, v in (st.get("pages") or {}).items() if k in PAGES and isinstance(v, dict)} \
        if isinstance(st.get("pages"), dict) else {}
    page = st.get("page") if st.get("page") in PAGES else panel if panel in PAGES else "overview"
    sub = {**inv, "state": pages.get(page) or {}}
    ev = Ctx(inv, False)
    to = ev.changed("nav") or ev.pressed("nav-")
    if to in PAGES:  # a tab: the page as it was left, the form of the one before set aside
        page, sub = to, {"kind": "open-panel", "state": pages.get(to) or {}}
    why = None
    for tight in (False, True):
        shown, root, state = render(page, sub, pages, tight)
        live = bool(state.pop("_live", False))
        out = {"page": shown, "pages": {**pages, shown: state}}
        if len(json.dumps(out)) > state_bytes():
            out = {"page": shown, "pages": {shown: state}}
        r = wrap(panel, frame(nav(shown), root), out, LIVE_MS if live else refresh_ms())
        why = problems(r["panels"][0]["root"]) or (f"{size(r)} bytes" if size(r) > max_bytes() else None)
        if not why:
            return r
    return wrap(panel, frame(nav(page), failed(page, f"too much to show here ({why})", "kx status")), {"page": page}, refresh_ms())


def frame(top: dict, page: dict) -> dict:
    """The tabs above the page. A page that is a sidebar layout (API 4) is the panel's root, the tabs at the top of
    its growing column: NextCode 0.1.6 then fills the panel with its columns and scrolls each on its own."""
    if is_sidebar(page):
        main = next(c for c in page["children"] if c.get("grow"))
        main["children"].insert(0, top)
        return page
    return col("app", top, page, gap=12)


def render(page: str, sub: dict, pages: dict, tight: bool) -> tuple[str, dict, dict]:
    """A page drawn: (the page, its root, its state); a link to another page draws that one."""
    for _ in range(3):
        ctx = Ctx(sub, tight)
        try:
            root, state = views()[page](ctx)
            return page, root, state
        except Go as go:
            page, sub = go.page, {"kind": "open-panel", "state": pages.get(go.page) or {}, "args": go.with_}
        except KxError as e:
            return page, failed(page, str(e), e.fix), ctx.state
        except Exception as e:  # noqa: BLE001 - the panel shows the error instead of a dead one
            return page, failed(page, f"{type(e).__name__}: {e}", None), ctx.state
    return page, failed(page, "pages sent to each other in a loop", None), {}


def wrap(panel: str, root: dict, state: dict, every_ms: int) -> dict:
    state = state if len(json.dumps(state)) <= state_bytes() else {}
    return {"panels": [{"id": panel, "root": root, "state": state, "refresh_ms": every_ms}]}


def size(r: dict) -> int:
    return len(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode())


def nav(page: str) -> dict:
    """The tabs, and Refresh."""
    return row("top", tabs("nav", page, list(PAGES.items())), button("refresh", "Refresh"), gap=24)


def header(title: str, sub: str = "", *extra) -> dict:
    """A line on what the page shows (its title too when the tab doesn't say it), and controls of the page's own."""
    text = sub if title in PAGES.values() and sub else f"**{esc(title)}**" + (f" · {sub}" if sub else "")
    return row("head", md("title", text), *extra, gap=16)


def footer() -> dict:
    from kx.views.nextcode.widgets import label
    return label("updated", f"Updated {datetime.now():%H:%M:%S}")


def failed(panel: str, error: str, fix: str | None) -> dict:
    return col("root", header(PAGES.get(panel, panel)),
               md("error", f"**{esc(error)}**" + (f"\n\nfix: `{tick(fix)}`" if fix else "")))


def comp():
    return config.load()


def run_kx(argv: list[str]):
    """A kx command's data, as its --json prints it (in-process, as the web dashboard does)."""
    from kx.views.dashboard import run_kx as run
    return run(argv)


def main(invocation: str | None) -> dict:
    try:
        inv = json.loads(invocation) if invocation else None
    except json.JSONDecodeError as e:
        raise KxError(f"--invocation is not JSON: {e}") from None
    return response(inv if isinstance(inv, dict) else None)
