"""Text and JSON output. Text is plain aligned columns (ANSI bold only on a terminal); JSON is one document."""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
from datetime import datetime, timezone

from kx.ledger.store import parse_ts

TTY = sys.stdout.isatty()


def width() -> int:
    """Terminal width on a terminal; wide (no wrapping, nothing cut) otherwise."""
    return shutil.get_terminal_size((100, 40)).columns if TTY else 240


def bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if TTY and not os.environ.get("NO_COLOR") else s


def dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if TTY and not os.environ.get("NO_COLOR") else s


def trunc(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


def dur(seconds) -> str:
    if seconds is None:
        return "-"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}"
    return f"{s // 86400}d{s % 86400 // 3600}h"


def ago(ts) -> str:
    d = parse_ts(ts)
    if d is None:
        return "-"
    return dur((datetime.now(timezone.utc) - d).total_seconds())


def num(v, digits: int = 4) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if not math.isfinite(v):
            return str(v)
        return f"{v:.{digits}g}" if abs(v) < 1e5 else f"{v:,.0f}"
    return str(v)


def score(value, se=None, digits: int = 4) -> str:
    return num(value, digits) + (f" ±{num(se, 2)}" if se is not None else "")


def table(rows: list[list], headers: list[str] | None = None, *, flex: int | None = None, indent: int = 0) -> str:
    """Aligned columns. On a terminal the `flex` column (default: the last) is cut to fit the width."""
    rows = [["" if c is None else str(c) for c in r] for r in rows]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows + ([headers] if headers else []))
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    allr = ([list(headers) + [""] * (ncol - len(headers))] if headers else []) + rows
    widths = [max(len(r[i]) for r in allr) for i in range(ncol)]
    flex = ncol - 1 if flex is None else flex
    avail = width() - indent - sum(w + 2 for i, w in enumerate(widths) if i != flex)
    if TTY and widths[flex] > max(avail, 12):
        widths[flex] = max(avail, 12)
    out = []
    for j, r in enumerate(allr):
        cells = [trunc(c, widths[i]) if i == flex else c for i, c in enumerate(r)]
        line = "  ".join(c.ljust(widths[i]) if i < ncol - 1 else c for i, c in enumerate(cells)).rstrip()
        out.append(" " * indent + (bold(line) if headers and j == 0 else line))
    return "\n".join(out)


def emit_json(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=_default, ensure_ascii=False) + "\n")


def _default(o):
    if hasattr(o, "keys"):
        return dict(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def kv(d: dict, indent: int = 2, skip_private: bool = True) -> str:
    """key: value lines (lists on one line each), for previews."""
    lines = []
    keys = [k for k in d if not (skip_private and str(k).startswith("_")) and d[k] not in (None, "", [], {})]
    w = max((len(str(k)) for k in keys), default=0)
    for k in keys:
        v = d[k]
        if isinstance(v, list):
            v = "; ".join(map(str, v))
        elif isinstance(v, dict):
            v = ", ".join(f"{a}={b}" for a, b in v.items())
        lines.append(" " * indent + f"{str(k).ljust(w)}  {v}")
    return "\n".join(lines)
