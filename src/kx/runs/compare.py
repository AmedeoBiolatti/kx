"""Is run B better than run A? Only answered under a shared protocol."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from kx.ledger.config import DEFAULT_Z
from kx.ledger.store import Store, loads
from kx.runs.protocol import ranking, reference, spec_diff, split, split_text

Z_THRESHOLD = DEFAULT_Z


# -- small-sample statistics (no scipy) ---------------------------------------


def _betacf(a: float, b: float, x: float, itmax: int = 500, eps: float = 1e-15) -> float:
    """Continued fraction for the incomplete beta function (Numerical Recipes, Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c
        c = c if abs(c) > tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1 - math.exp(lbt) * _betacf(b, a, 1 - x) / b


def t_sf(t: float, df: float) -> float:
    """One-sided P(T > |t|) for Student's t with df degrees of freedom."""
    return 0.5 * _betai(df / 2, 0.5, df / (df + t * t))


def t_to_z(t: float, df: float) -> float:
    """The normal z with the same one-sided p-value as t, so one threshold works for any n."""
    if math.isinf(t) or t == 0:
        return t
    p = t_sf(t, df)
    z = math.inf if p <= 0 else -statistics.NormalDist().inv_cdf(p)
    return math.copysign(z, t)


def cv_factor(k: int) -> float:
    """Nadeau-Bengio: var(mean of k CV folds) ≈ var * (1/k + n_test/n_train), n_test/n_train = 1/(k-1)."""
    return math.sqrt(1 / k + 1 / (k - 1)) * math.sqrt(k)  # relative to the naive sd/sqrt(k)


def se_from_breakdown(breakdown: dict | None, noise: str = "independent") -> tuple[float | None, int | None]:
    vals = [v for v in (breakdown or {}).values() if isinstance(v, (int, float))]
    if len(vals) < 2:
        return None, (len(vals) or None)
    se = statistics.stdev(vals) / math.sqrt(len(vals))
    if noise == "cv":
        se *= cv_factor(len(vals))
    return se, len(vals)


@dataclass
class Comparison:
    protocol: str
    direction: str
    a: float
    b: float
    delta: float  # b - a, raw
    z: float | None  # positive = b better, whatever the direction (normal-equivalent for paired t)
    method: str
    verdict: str  # better | worse | within noise | unknown noise | inconsistent

    @property
    def symbol(self) -> str:
        return {"better": "▲", "worse": "▼", "within noise": "≈", "inconsistent": "!"}.get(self.verdict, "?")


@dataclass
class Result:
    a: int
    b: int
    comparisons: list[Comparison] = field(default_factory=list)
    only_a: list[str] = field(default_factory=list)
    only_b: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    @property
    def comparable(self) -> bool:
        return bool(self.comparisons)


def _signed_ratio(num: float, se: float) -> float:
    if se > 0:
        return num / se
    return math.copysign(math.inf, num) if num else 0.0


def compare_scores(sa, sb, direction: str, z_thr: float = Z_THRESHOLD, noise: str = "independent") -> Comparison:
    sign = 1 if direction == "max" else -1
    delta = sb["value"] - sa["value"]
    ba, bb = loads(sa["breakdown"]) or {}, loads(sb["breakdown"]) or {}
    shared = [k for k in ba if k in bb and isinstance(ba[k], (int, float)) and isinstance(bb[k], (int, float))]
    z, method = None, "no noise estimate"
    mismatched = bool(ba and bb and set(ba) != set(bb))
    inconsistent = mismatched
    if len(shared) >= 2:
        d = [bb[k] - ba[k] for k in shared]
        n, mean = len(d), statistics.fmean(d)
        se = statistics.stdev(d) / math.sqrt(n)
        if noise == "cv":
            se *= cv_factor(n)
        z = t_to_z(_signed_ratio(sign * mean, se), n - 1)
        method = f"paired t, n={n}" + (", cv-corrected" if noise == "cv" else "")
        # the shown delta comes from the values, the test from the breakdown: they must agree in sign
        inconsistent = mismatched or (mean * delta < 0 and abs(mean) > 1e-12 and abs(delta) > 1e-12)
    elif sa["se"] is not None and sb["se"] is not None:
        z = _signed_ratio(sign * delta, math.hypot(sa["se"], sb["se"]))
        method = "unpaired (se)"
    if inconsistent:
        verdict = "inconsistent"
        method += "; sample schedules differ" if mismatched else "; value Δ and breakdown Δ disagree in sign"
    elif z is None:
        verdict = "unknown noise"
    elif z > z_thr:
        verdict = "better"
    elif z < -z_thr:
        verdict = "worse"
    else:
        verdict = "within noise"
    return Comparison(sa["protocol_id"], direction, sa["value"], sb["value"], delta, z, method, verdict)


def compare(store: Store, a: int, b: int, z_thr: float = Z_THRESHOLD) -> Result:
    sa = {s["protocol_id"]: s for s in store.scores_for(a)}
    sb = {s["protocol_id"]: s for s in store.scores_for(b)}
    res = Result(a, b)
    for pid in sa:
        if pid in sb:
            res.comparisons.append(compare_scores(sa[pid], sb[pid], sa[pid]["direction"], z_thr, sa[pid]["noise"]))
    res.only_a = [p for p in sa if p not in sb]
    res.only_b = [p for p in sb if p not in sa]
    # explain near misses: same protocol name, different fingerprint
    for pa in res.only_a:
        for pb in res.only_b:
            if sa[pa]["name"] == sb[pb]["name"]:
                diff = spec_diff(loads(sa[pa]["spec"]), loads(sb[pb]["spec"]))
                if sa[pa]["noise"] != sb[pb]["noise"]:
                    diff.append(f"noise: {sa[pa]['noise']} -> {sb[pb]['noise']}")
                res.hints.append(
                    f"{a} has {pa}, {b} has {pb}: same name, different spec ({'; '.join(diff)}). "
                    f"Re-score one of them under the other's protocol."
                )
    return res


def outcome(store: Store, run_id: str) -> dict | None:
    """What a run that has just ended (or reached a target) came to, so whoever waited for it need not ask: its
    scores, its rank under the reference protocol, and the verdict against its parent (when they share a protocol)
    or else the best other run under the reference. None while it has no score."""
    scores = store.scores_for(run_id)
    if not scores:
        return None
    ref = reference(store)
    out = {"scores": [{"protocol": s["protocol_id"], "value": s["value"], "se": s["se"]} for s in scores],
           "reference": ref["id"]}
    mine = next((s for s in scores if s["protocol_id"] == ref["id"]), None)
    others = []  # the other done runs under the reference, best first
    if mine is not None:
        others = [r for r in ranking(store, ref["id"]) if r["id"] != run_id]
        sign = 1 if mine["direction"] == "max" else -1
        out["rank"] = 1 + sum(1 for r in others if sign * (r["value"] - mine["value"]) > 0)
        out["of"] = len(others) + 1
    base, why = None, None
    parent = store.get_run(run_id)["parent"]
    if parent and compare(store, parent, run_id).comparable:
        base, why = parent, "parent"
    elif others:
        base, why = others[0]["id"], "best"
    if base is not None:
        res = compare(store, base, run_id, store.comp.z)
        c = next((c for c in res.comparisons if c.protocol == ref["id"]), res.comparisons[0])
        out["vs"] = {"run": base, "as": why, **c.__dict__, "symbol": c.symbol}
    if splits := [sp for s in scores if (sp := split(store, s["protocol_id"]))]:
        out["split"] = splits
    return out


def outcome_line(o: dict) -> str:
    """outcome() in one line: 'cv5@ab12 0.8134 ±0.0021 · #2 of 41 · vs r0012 (best): +0.0031, z=+2.40 ▲ better'."""
    from kx.views.text import score
    main = next((s for s in o["scores"] if s["protocol"] == o["reference"]), o["scores"][0])
    parts = [f"{main['protocol']} {score(main['value'], main['se'])}"]
    if len(o["scores"]) > 1:
        parts[0] += f" (+{len(o['scores']) - 1} other protocol{'s' if len(o['scores']) > 2 else ''})"
    if "rank" in o:
        parts.append(f"#{o['rank']} of {o['of']}")
    elif o["reference"]:
        parts.append(f"not scored under the reference {o['reference']}")
    if v := o.get("vs"):
        parts.append(f"vs {v['run']} ({v['as']}): {v['delta']:+.4g}" + (f", z={v['z']:+.2f}" if v["z"] is not None else "")
                     + f" {v['symbol']} {v['verdict']}")
    return " · ".join(parts) + "".join(f"\n  ! {split_text(sp)}" for sp in o.get("split", []))
