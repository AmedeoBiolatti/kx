"""What `kx wait TARGET ...` waits for: a run to end, or one of its metrics to reach a value (`--until`); a submission
to be scored; a kernel version to finish. Each check reads the state afresh (the ledger, the run's files, Kaggle), so
a wait holds nothing: run it again after a timeout or a lost session and it answers at once if it has happened.

A wait also returns when what it waits for can no longer happen: the run failed, ended short of the target, went
silent, or lost its process; the submission or the kernel errored. kx wait exits 0 when it happened, UNMET otherwise.
"""

from __future__ import annotations

import json
import math
import operator
import re
import sys
import time

from kx.ledger import config
from kx.ledger.config import KxError
from kx.ledger.store import Store, age_s, loads
from kx.runs.compare import outcome, outcome_line
from kx.runs.progress import number, records

UNMET = 6                    # exit code: it ended (or stalled) without what was waited for
KAGGLE_EVERY_S = (60, 300)   # Kaggle is asked at most this often by one wait, backing off from the first to the second
ACTIVITY = ("progress.json", "metrics.jsonl", "stdout.log", "stderr.log")  # a run that touches none of them is silent

OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "==": operator.eq}
COND = re.compile(r"^\s*(?P<name>.+?)\s*(?P<op>>=|<=|==|>|<)\s*(?P<value>\S+)\s*$")


def condition(text: str) -> tuple[str, str, float]:
    """'val_auc>=0.91' -> ('val_auc', '>=', 0.91)."""
    m = COND.match(text)
    try:
        value = float(m["value"]) if m else math.nan
    except ValueError:
        value = math.nan
    if not m or not math.isfinite(value):
        raise KxError(f"not a condition: {text!r}", fix="NAME OP NUMBER, OP one of >= <= > < ==: e.g. val_auc>=0.91, "
                                                        "train/loss<0.2, cv5>=0.8 (a score under protocol cv5)", code=2)
    return m["name"], m["op"], value


def _later(every: float) -> float:
    return min(every * 1.5, KAGGLE_EVERY_S[1])


class RunTarget:
    """A run: it ends (done = met), or with conditions, one of them holds for a value the run logged (r.step, r.log,
    r.progress metrics, a score under a protocol named so, `score` for the reference protocol's)."""

    def __init__(self, run_id: str, conds=(), stall_s: float = 0):
        self.id, self.name, self.conds, self.stall_s = run_id, run_id, list(conds), stall_s
        self.remote_state = None

    def _values(self, st: Store, row) -> dict[str, list[tuple[float, float | None]]]:
        """Every value the run logged, by name, in order: (value, step)."""
        out: dict[str, list] = {}
        for rec in records(st.run_dir(self.id) / "metrics.jsonl"):
            step = rec.get("step") if number(rec.get("step")) else None
            for k, v in rec.items():
                if k not in ("step", "ts") and number(v):
                    out.setdefault(k, []).append((float(v), step))
        summary = loads(row["metrics"]) or {}
        for k, v in (summary.items() if isinstance(summary, dict) else ()):
            if number(v):
                out.setdefault(k, []).append((float(v), None))
        from kx.runs.protocol import reference
        ref = reference(st)["id"]
        for s in st.scores_for(self.id):
            for k in {s["name"], s["protocol_id"], *(["score"] if s["protocol_id"] == ref else [])}:
                out.setdefault(k, []).append((float(s["value"]), None))
        return out

    def _met(self, values) -> dict | None:
        for name, op, target in self.conds:
            hits = [(v, step) for v, step in values.get(name, []) if OPS[op](v, target)]
            if hits:
                v, step = hits[-1]
                return {"condition": f"{name}{op}{target:g}", "metric": name, "value": v, "step": step}
        return None

    def _sync_remote(self):
        from kx.remote import execute
        try:
            items = execute.sync(self.name)
        except KxError as e:
            self.remote_state = f"not synced: {str(e).splitlines()[0]}"
            return
        self.remote_state = items[0]["state"] if items else None

    def _silent_s(self, st: Store, row) -> float:
        d = st.run_dir(self.id)
        last = max([(d / f).stat().st_mtime for f in ACTIVITY if (d / f).exists()], default=None)
        return time.time() - last if last is not None else (age_s(row["created"]) or 0)

    def check(self) -> dict | None:
        comp = config.load()
        with Store(comp) as st:
            row = st.get_run(self.id)
            remote = (st.run_dir(self.id) / "remote.json").exists()
        if remote and row["status"] == "running":
            self._sync_remote()  # a remote run's files and its end only arrive by copying them back
        with Store(comp) as st:
            row = st.get_run(self.id)
            ev = {"target": self.name, "hypothesis": row["hypothesis"], "status": row["status"]}
            if self.conds and (hit := self._met(self._values(st, row))):
                return {**ev, "event": "met", "met": True, **hit, "result": outcome(st, self.id),
                        "next": f"kx show {self.name}"}
            if row["status"] != "running":
                ev.update(event=row["status"], met=row["status"] == "done" and not self.conds,
                          error=(row["error"] or "")[-600:] or None, result=outcome(st, self.id),
                          next=f"kx show {self.name}")
                if self.conds:
                    values = self._values(st, row)
                    ev["unmet"] = [f"{n}{op}{v:g}" for n, op, v in self.conds]
                    ev["last"] = {n: values[n][-1][0] for n, _, _ in self.conds if n in values}
                    if not ev["last"]:
                        ev["logged"] = sorted(values)
                return ev
            gone = next((why for r, why in st.stale_runs() if r["id"] == self.id), None)
            if gone:
                return {**ev, "event": "gone", "met": False, "why": gone,
                        "next": f"kx reap (records it as failed), then kx show {self.name}"}
            silent = self._silent_s(st, row)
            if self.stall_s and silent >= self.stall_s:
                return {**ev, "event": "stalled", "met": False, "silent_s": round(silent),
                        **({"remote": self.remote_state} if self.remote_state else {}),
                        "next": f"kx show {self.name}; " + (f"kx remote kill {self.name}" if remote else
                                                             f"stop it: kill {row['pid']}")
                                + "; or wait longer: the same kx wait with --stall 0 (or more minutes)"}
        return None


class SubmissionTarget:
    """A submission: scored on Kaggle (met), or errored. Kaggle is asked through `kx sync`'s own code, at most every
    KAGGLE_EVERY_S, and not at all while another wait (or kx sync) has just asked."""

    def __init__(self, sub_id: str):
        self.id, self.name = sub_id, sub_id
        self.every, self.asked = KAGGLE_EVERY_S[0], None

    @staticmethod
    def _settled(s) -> dict | None:
        ev = {"target": s["id"], "status": s["status"], "public": s["public"], "private": s["private"],
              "run": s["run_id"], "expect": s["expect"], "next": f"kx subs {s['id']}"}
        if s["status"] == "error":
            return {**ev, "event": "error", "met": False, "error": s["error"]}
        if s["status"] == "complete" or s["public"] is not None:
            return {**ev, "event": "scored", "met": True}
        return None

    def check(self) -> dict | None:
        from kx.kaggle import submit
        with Store(config.load()) as st:
            s = submit.find(st, self.name)
            if ev := self._settled(s):
                return ev
            if self.asked is not None and time.monotonic() - self.asked < self.every:
                return None
            synced = age_s(st.setting("synced"))
            if synced is not None and synced < self.every:
                return None  # someone else asked Kaggle just now: their answer is in the ledger
            self.asked, self.every = time.monotonic(), _later(self.every)
            try:
                submit.sync_submissions(st, games=False)
            except KxError as e:
                print(f"kx: Kaggle not reached ({str(e).splitlines()[0]}); trying again", file=sys.stderr)
                return None
            return self._settled(submit.find(st, self.name))


class KernelTarget:
    """The kernel version pushed for a run (kx kernel push R): complete (met), or error / cancelled."""

    def __init__(self, run_id: str):
        self.id, self.name = run_id, f"kernel:{run_id}"
        self.every, self.asked = KAGGLE_EVERY_S[0], None
        with Store(config.load()) as st:
            row = st.one("SELECT target, version FROM pushes WHERE kind='kernel' AND run_id=? ORDER BY id DESC LIMIT 1",
                         (run_id,))
        if row is None:
            raise KxError(f"no kernel pushed for {run_id}", fix=f"kx kernel push {run_id}")
        self.kernel, self.version = row["target"], row["version"]

    def check(self) -> dict | None:
        from kx.kaggle import client as kaggle
        if self.asked is not None and time.monotonic() - self.asked < self.every:
            return None
        self.asked, self.every = time.monotonic(), _later(self.every)
        try:
            status, _ = kaggle.kernel_status(self.kernel, self.version)
        except KxError as e:
            print(f"kx: Kaggle not reached ({str(e).splitlines()[0]}); trying again", file=sys.stderr)
            return None
        ev = {"target": self.name, "kernel": self.kernel, "version": self.version, "status": status,
              "next": f"kx kernel status {self.id}"}
        if status == "complete":
            return {**ev, "event": "complete", "met": True}
        if status == "error" or status.startswith("cancel"):
            return {**ev, "event": status, "met": False}
        return None


def targets(refs, until=(), stall_s: float = 0) -> list:
    """r12 / run:r12 / latest / best, S12 / submission:S12, kernel:r12, group:NAME (its runs, as they are now)."""
    conds = [condition(c) for c in until]
    out = []
    with Store(config.load()) as st:
        for ref in refs:
            kind, _, val = ref.partition(":") if ":" in ref else ("", "", ref)
            kind = kind.lower()
            if kind == "kernel":
                out.append(KernelTarget(st.resolve(val)))
            elif kind == "group":
                ids = [r["id"] for r in st.all("SELECT id FROM runs WHERE grp=? ORDER BY n, id", (val,))]
                if not ids:
                    raise KxError(f"no runs in group {val!r}", fix="runs join a group with kx exec --group NAME")
                out += [RunTarget(i, conds, stall_s) for i in ids if i not in {t.id for t in out if isinstance(t, RunTarget)}]
            elif kind in ("submission", "sub") or (not kind and re.fullmatch(r"[Ss]\d+(?:-[a-z]+)*(?:~\w+)?", val)):
                from kx.kaggle import submit
                out.append(SubmissionTarget(submit.find(st, val if val[:1] in "Ss" or not val.isdigit() else f"S{val}")["id"]))
            elif kind in ("", "run"):
                run_id = st.resolve(val)
                st.get_run(run_id)
                out.append(RunTarget(run_id, conds, stall_s))
            else:
                raise KxError(f"cannot wait for {ref!r}", fix="a run (r12), a group of runs (group:NAME), a submission (S4) "
                                                              "or a kernel (kernel:r12)", code=2)
    if conds and not any(isinstance(t, RunTarget) for t in out):
        raise KxError("--until is about a run's metrics: give a run to wait for", fix="kx wait r12 --until 'val_auc>=0.9'",
                      code=2)
    return out


def line(ev: dict) -> str:
    """One event as a sentence, with what to run next."""
    t, e = ev["target"], ev["event"]
    if e == "met":
        what = f"{t}: {ev['condition']} ({ev['metric']} = {ev['value']:g}" + \
               (f" at step {ev['step']:g})" if ev.get("step") is not None else ")")
    elif e == "scored":
        what = f"{t} scored: public {ev['public']}" + (f" (expected: {ev['expect']})" if ev.get("expect") else "")
    elif e == "error" and t.startswith("S"):
        what = f"{t} failed on Kaggle: {ev.get('error') or 'no reason given'}"
    elif e in ("done", "failed"):
        what = f"{t} {e}"
        if ev.get("unmet"):
            what += " without " + " or ".join(ev["unmet"])
            what += f" (last: {json.dumps(ev['last'])})" if ev.get("last") else \
                f" (it never logged that; it logs: {', '.join(ev.get('logged') or []) or 'nothing'})"
        if e == "failed" and ev.get("error"):
            what += f": {ev['error'].strip().splitlines()[0][:200]}"
    elif e == "gone":
        what = f"{t} lost its process ({ev['why']})"
    elif e == "stalled":
        what = f"{t} silent for {ev['silent_s'] // 60} min (no progress, metrics or output)" + \
               (f"; host: {ev['remote']}" if ev.get("remote") else "")
    else:
        what = f"{t} {e}"
    if ev.get("result"):
        what += "\n  " + outcome_line(ev["result"])
    return f"{what}\n  next: {ev['next']}"
