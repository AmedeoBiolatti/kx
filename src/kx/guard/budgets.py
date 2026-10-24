"""Caps on what costs something: submissions, Kaggle GPU hours, local GPU hours, worker tokens, paid remote hosts.

comp.yaml `budgets:` holds team caps (`<metric>_per_<window>`) and per-worker caps (`workers: {name: {...}}`). A cap is
checked before the action; an action over a cap is refused with when it frees up (exit code 4): wait, or raise the
cap. Kaggle's own limits (daily submissions) are not budgets and always apply.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kx.ledger.config import KxError
from kx.ledger.store import Store, now, parse_ts, since

METRICS = {
    "submissions": "submissions",
    "kaggle_gpu_hours": "Kaggle GPU hours",
    "gpu_hours": "local GPU hours",
    "tokens": "worker tokens",
    "cloud_usd": "paid remote hosts (USD)",  # kx exec --on HOST: wall time x the host's usd_per_hour
}
WINDOWS = {"day": "today (UTC)", "week": "in the last 7 days"}


def parse_key(key: str) -> tuple[str, str] | None:
    metric, sep, window = key.rpartition("_per_")
    return (metric, window) if sep and metric in METRICS and window in WINDOWS else None


def validate(budgets: dict, where) -> None:
    def check(d: dict, ctx: str):
        for k, v in d.items():
            if parse_key(k) is None:
                raise KxError(f"{where}: {ctx}{k} is not a budget",
                              fix="use <metric>_per_<day|week> with metric one of " + ", ".join(METRICS))
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise KxError(f"{where}: {ctx}{k} must be a non-negative number")
    check({k: v for k, v in budgets.items() if k != "workers"}, "budgets.")
    workers = budgets.get("workers") or {}
    if not isinstance(workers, dict):
        raise KxError(f"{where}: budgets.workers must map worker names to caps")
    for name, caps in workers.items():
        if not isinstance(caps, dict):
            raise KxError(f"{where}: budgets.workers.{name} must be a mapping")
        check(caps, f"budgets.workers.{name}.")


def caps(store: Store, actor=None) -> list[dict]:
    """The caps that apply to actor (team caps always; its own when it is a worker with some)."""
    b = store.comp.budgets
    out = [{"scope": "team", "who": None, "key": k, "metric": parse_key(k)[0], "window": parse_key(k)[1], "cap": float(v)}
           for k, v in b.items() if k != "workers"]
    if actor is not None and actor.worker:
        for k, v in ((b.get("workers") or {}).get(actor.name) or {}).items():
            m, w = parse_key(k)
            out.append({"scope": actor.name, "who": actor.name, "key": k, "metric": m, "window": w, "cap": float(v)})
    return out


def used(store: Store, metric: str, window: str, who: str | None = None) -> float:
    sql, params = "SELECT COALESCE(SUM(amount), 0) FROM usage WHERE metric=? AND ts >= ?", [metric, since(window)]
    if who:
        sql += " AND actor=?"
        params.append(who)
    return float(store.one(sql, params)[0])


def frees_up(store: Store, metric: str, window: str, who: str | None = None) -> str:
    t = datetime.now(timezone.utc)
    if window == "day":
        reset = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return f"resets at 00:00 UTC (in {_dur((reset - t).total_seconds())})"
    sql, params = "SELECT ts FROM usage WHERE metric=? AND ts >= ?", [metric, since(window)]
    if who:
        sql += " AND actor=?"
        params.append(who)
    row = store.one(sql + " ORDER BY ts LIMIT 1", params)
    oldest = parse_ts(row["ts"]) if row else None
    if oldest is None:
        return "rolling 7 days"
    return f"rolling 7 days: the oldest use ages out in {_dur((oldest + timedelta(days=7) - t).total_seconds())}"


def _dur(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 86400}d{s % 86400 // 3600}h" if s >= 86400 else f"{s // 3600}h{s % 3600 // 60:02d}m"


def fmt(metric: str, v: float) -> str:
    if metric == "tokens":
        return f"{v / 1e6:.1f}M" if v >= 1e5 else f"{v:,.0f}"
    if metric == "cloud_usd":
        return f"${v:.2f}"
    if metric.endswith("hours"):
        return f"{v:.1f}h"
    return f"{v:g}"


def report(store: Store, actor=None) -> list[dict]:
    out = []
    for c in caps(store, actor):
        u = used(store, c["metric"], c["window"], c["who"])
        out.append({**c, "used": u, "left": max(0.0, c["cap"] - u),
                    "frees_up": frees_up(store, c["metric"], c["window"], c["who"])})
    return out


def check(store: Store, actor, cost: dict) -> None:
    """Refuse (KxError code 4) when the cost would go past a cap."""
    over = []
    for c in caps(store, actor):
        amount = cost.get(c["metric"])
        if not amount:
            continue
        u = used(store, c["metric"], c["window"], c["who"])
        if u + amount > c["cap"] + 1e-9:
            over.append(f"{c['scope']} {c['key']}={fmt(c['metric'], c['cap'])}: {fmt(c['metric'], u)} used "
                        f"{WINDOWS[c['window']]}, this needs {fmt(c['metric'], amount)}; "
                        f"{frees_up(store, c['metric'], c['window'], c['who'])}")
    if over:
        raise KxError("over budget: " + "; ".join(over), code=4,
                      fix="wait for it to free up" if actor.worker else "wait, or raise the cap in comp.yaml budgets:",
                      data={"over": over})


def charge(store: Store, actor, metric: str, amount: float, ref: str | None = None):
    if amount:
        store.x("INSERT INTO usage VALUES (?,?,?,?,?)", (now(), actor.name, metric, float(amount), ref))
