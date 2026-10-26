"""data.json for the video, from kx's --json output only: re-run it and every number in the video follows the ledger.

Usage (in the competition repo, or anywhere below it):
    python prep_data.py [--run R] [--baseline B] [--depth 12] [--curves loss,val_auc] [--findings 12]

--run: the run the video is about (default: the best under the reference protocol). Its lineage follows `parent`
back --depth runs. --baseline: what it is compared with (default: its parent, else the second best). --curves: the
metrics to plot (default: up to three both runs log, losses and validation metrics first). KX="python -m kx" runs
another kx.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
KX = shlex.split(os.environ.get("KX", "kx"))
ASKED = []  # the commands data.json comes from, kept in it


def kx(*args):
    cmd = [*KX, *args, "--json"]
    ASKED.append(shlex.join(["kx", *args, "--json"]))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode not in (0, 5):  # 5: kx compare found no shared protocol; it still answers in JSON
        sys.exit(f"{shlex.join(cmd)} failed:\n{(p.stderr or p.stdout).strip()[-800:]}")
    return json.loads(p.stdout)


def short(text, n=48):
    """A label cut from a hypothesis: its first clause, at a word boundary."""
    text = " ".join((text or "").split())
    for sep in (": ", "; ", ". ", " (", ", "):
        if 12 <= text.find(sep) < n:
            text = text[:text.find(sep)]
            break
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def entry(r, reference):
    """A run as the video uses it: its score under the reference protocol, else its first score."""
    scores = r.get("scores") or []
    s = next((x for x in scores if x["protocol"] == reference), scores[0] if scores else None)
    return {"run": r["run"], "label": short(r.get("hypothesis")), "hypothesis": r.get("hypothesis"),
            "status": r.get("status"), "score": s and s["value"], "se": s and s.get("se"), "protocol": s and s["protocol"],
            "created": r.get("created"), "finished": r.get("finished"), "git_sha": r.get("git_sha"),
            "parent": r.get("parent"), "task": r.get("task"), "config": r.get("config"), "tags": r.get("tags") or []}


PREFER = ("loss", "val", "valid", "auc", "acc", "score", "rmse", "mae", "nll", "f1")


def pick_curves(runs, wanted):
    """The metrics to plot: those asked for, else up to three every run logs, the telling ones first."""
    have = [set(r.get("curve_points") or {}) for r in runs]
    shared = set.intersection(*have) if have else set()
    if wanted:
        return [k for k in wanted if any(k in h for h in have)]
    ranked = sorted(shared or set.union(*have) if have else set(),
                    key=lambda k: (not any(w in k.lower() for w in PREFER), k))
    return ranked[:3]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run")
    ap.add_argument("--baseline")
    ap.add_argument("--depth", type=int, default=12)
    ap.add_argument("--curves", type=lambda s: [x for x in s.split(",") if x])
    ap.add_argument("--findings", type=int, default=12)
    a = ap.parse_args()

    ctx = kx("context")
    reference = (ctx.get("reference") or {}).get("id")
    rows = kx("board").get("rows") or []
    target = a.run or (rows[0]["run"] if rows else None)
    if not target:
        sys.exit("no scored run under the reference protocol: pass --run R")
    best = kx("show", target, "--curves")

    chain = [best]
    while chain[-1].get("parent") and len(chain) < a.depth:
        chain.append(kx("show", chain[-1]["parent"]))
    chain.reverse()

    base_id = a.baseline or best.get("parent") or next((r["run"] for r in rows if r["run"] != best["run"]), None)
    base = kx("show", base_id, "--curves") if base_id else None
    comparison = None
    if base:
        cmp = kx("compare", base["run"], best["run"])
        found = cmp.get("comparisons") or []
        comparison = next((c for c in found if c["protocol"] == reference), found[0] if found else None)

    plotted = [r for r in (base, best) if r]
    names = pick_curves(plotted, a.curves)
    curves = {r["run"]: {k: r["curve_points"][k] for k in names if k in (r.get("curve_points") or {})} for r in plotted}

    ids = {r["run"] for r in chain} | ({base["run"]} if base else set())
    findings = [{"id": f["id"], "text": f["text"], "status": f["status"], "evidence": f.get("evidence") or []}
                for f in kx("finding", "list", "--all") if ids & set(f.get("evidence") or [])][:a.findings]
    subs = [{"id": s["id"], "run": s.get("run_id"), "status": s.get("status"), "public": s.get("public"),
             "private": s.get("private"), "created": s.get("created")}
            for s in kx("subs").get("submissions") or [] if s.get("run_id") in ids]

    c = ctx.get("competition") or {}
    data = {
        "made": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "from": ASKED,
        "competition": {k: c.get(k) for k in ("slug", "kind", "mode", "metric", "direction", "deadline")}
                       | {"title": (c.get("slug") or "").replace("-", " ").title()},
        "reference": reference,
        "best": entry(best, reference),
        "baseline": entry(base, reference) if base else None,
        "comparison": comparison,
        "lineage": [entry(r, reference) for r in chain],
        "curves": curves,
        "findings": findings,
        "submissions": subs,
    }
    (HERE / "data.json").write_text(json.dumps(data, indent=1, default=str))
    print(f"data.json: {best['run']}, {len(chain)} runs of lineage, baseline {base['run'] if base else 'none'}, "
          f"curves {names or 'none'}, {len(findings)} findings, {len(subs)} submissions")


if __name__ == "__main__":
    main()
