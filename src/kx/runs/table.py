"""Runs side by side: which runs (`r12 r14..r20 group:NAME latest best`) and what to show of each (`--cols`).

A column is any value kx or the run recorded, by name:

    score, score.se          the score under the reference protocol
    cv5, cv5.se, cv5.breakdown.f0   a score under a protocol, by name or id
    config.lr                the run's config
    val_auc                  a metric it logged (its latest value: r.step, r.log, r.progress)
    summary.json:by_deck.x   a JSON file the run wrote in its dir (a .jsonl file: its last record)

What a run lacks is None, not an error; a column no run has comes back with what is there instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kx.ledger.config import KxError
from kx.ledger.store import Store, loads
from kx.runs.progress import records, snapshot

RANGE = re.compile(r"^r?(\d+)(?:-[\w~-]+)?\.\.r?(\d+)(?:-[\w~-]+)?$", re.I)  # r12..r20, r0012-fluent-sun..r0020-…
MISSING = object()


def expand(st: Store, refs) -> list[str]:
    """Run ids for refs, in the order given, each once: r12, fluent-sun, latest, best, r12..r20 (the runs numbered
    from 12 to 20; from every machine), group:NAME (kx exec --group)."""
    out: list[str] = []
    for ref in refs:
        if m := RANGE.match(ref):
            lo, hi = sorted((int(m[1]), int(m[2])))
            ids = [r["id"] for r in st.all("SELECT id FROM runs WHERE n BETWEEN ? AND ? ORDER BY n, id", (lo, hi))]
            if not ids:
                raise KxError(f"no runs numbered {lo} to {hi}", fix="kx runs")
        elif ref.lower().startswith("group:"):
            ids = [r["id"] for r in st.all("SELECT id FROM runs WHERE grp=? ORDER BY n, id", (ref[6:],))]
            if not ids:
                raise KxError(f"no runs in group {ref[6:]!r}", fix="runs join a group with kx exec --group NAME")
        else:
            ids = [st.resolve(ref)]
            st.get_run(ids[0])
        out += [i for i in ids if i not in out]
    return out


def walk(value, path: str):
    """value[a][b]... for path 'a.b...'; keys may hold dots themselves ('lr.0.1' finds the key 'lr.0.1')."""
    parts = path.split(".") if path else []
    i = 0
    while i < len(parts):
        if isinstance(value, dict):
            for j in range(len(parts), i, -1):  # the longest key that is there
                key = ".".join(parts[i:j])
                if key in value:
                    value, i = value[key], j
                    break
            else:
                return MISSING
        elif isinstance(value, list) and re.fullmatch(r"-?\d+", parts[i]) and -len(value) <= int(parts[i]) < len(value):
            value, i = value[int(parts[i])], i + 1
        else:
            return MISSING
    return value


def _file(d: Path, name: str):
    p = (d / name).resolve()
    if not p.is_relative_to(d.resolve()) or not p.is_file():
        return MISSING
    if p.suffix == ".jsonl":
        recs = records(p)
        return recs[-1] if recs else MISSING
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return MISSING


class Columns:
    """Reads the columns of runs, keeping what each run needs once (its scores, config, metrics, files)."""

    def __init__(self, st: Store, cols):
        self.st, self.cols = st, list(cols)
        from kx.runs.protocol import reference
        self.ref = reference(st)["id"]

    def sources(self, row) -> dict:
        scores = {}
        for s in self.st.scores_for(row["id"]):
            v = {"value": s["value"], "se": s["se"], "n": s["n"], "breakdown": loads(s["breakdown"])}
            scores[s["name"]] = scores[s["protocol_id"]] = v  # in time order: a name keeps its latest version
            if s["protocol_id"] == self.ref:
                scores["score"] = v
        return {"scores": scores, "config": loads(row["config"]) or {}, "dir": self.st.run_dir(row["id"]),
                "metrics": snapshot(self.st, row)["latest_metrics"]}

    def value(self, src: dict, col: str):
        if ":" in col and not col.startswith(":"):
            name, _, path = col.partition(":")
            got = _file(src["dir"], name)
            return got if got is MISSING else walk(got, path)
        if col in src["metrics"]:
            return src["metrics"][col]
        head, _, rest = col.partition(".")
        if head == "config":
            return walk(src["config"], rest)
        for name, sc in src["scores"].items():
            if col == name or col.startswith(name + "."):
                return walk(sc, col[len(name) + 1:] or "value")
        return MISSING

    def row(self, row) -> dict:
        src = self.sources(row)
        return {c: (None if (v := self.value(src, c)) is MISSING else v) for c in self.cols}

    def there(self, row, col: str) -> list[str]:
        """For a column the run lacks: the names next to it that it has (for 'not found' hints)."""
        src = self.sources(row)
        if ":" in col:
            name, _, path = col.partition(":")
            got = _file(src["dir"], name)
            if got is MISSING:
                return sorted(p.name for p in src["dir"].iterdir() if p.suffix in (".json", ".jsonl"))[:30] \
                    if src["dir"].is_dir() else []
            parts = path.split(".") if path else []
            for k in range(len(parts), -1, -1):  # the deepest level that is there
                v = walk(got, ".".join(parts[:k]))
                if v is not MISSING:
                    return [f"{name}:{'.'.join([*parts[:k], str(x)])}" for x in
                            (list(v)[:30] if isinstance(v, dict) else range(len(v)) if isinstance(v, list) else [])]
            return []
        if col.startswith("config."):
            return [f"config.{k}" for k in list(src["config"])[:30]]
        return sorted({*src["metrics"], *src["scores"], "config.…", "FILE.json:PATH"})[:40]


def cell(v, width: int = 40) -> str:
    """A value as a table cell."""
    from kx.views.text import num, trunc
    if v is None:
        return "–"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return num(v)
    if isinstance(v, dict):
        return trunc(", ".join(f"{k}={cell(x, 12)}" for k, x in v.items()), width)
    return trunc(v if isinstance(v, str) else json.dumps(v), width)


def split_cols(specs) -> list[str]:
    return [c.strip() for s in specs or () for c in s.split(",") if c.strip()]
