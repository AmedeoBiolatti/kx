"""What the person can do about each item of kx status's "needs you", as data every surface draws the same way: the
dashboard and the NextCode panel as buttons, bare `kx` as prompts, an agent as the commands they are.

Each item gets `who`:
- decision: only the person settles it (send what a worker asked for, answer it, merge a branch, retry a worker);
- chore: bookkeeping anyone may do, the agent you talk to included (close dead runs, copy a run back, ask Kaggle);
- advice: worth knowing (kx ranking under a protocol nobody chose, a budget used up): it waits for no one.

and `actions`: [{label, argv, primary?, preview?, input?, resume?}]. argv is a kx command line; `preview` one that
shows what argv would do (kx approve T7 before kx approve T7 --yes); `input` asks for text, added as the last
argument; `resume` a command offered afterwards (kx spawn T7 -w the worker that asked). The dashboard and the panel
run only what `allowed` accepts, through `execute`.
"""

from __future__ import annotations

import re

from kx.board import decide, tasks
from kx.ledger.config import KxError
from kx.ledger.store import Store

CHORES = ("dead_run", "remote_run", "uncertain_submission", "sync")
ADVICE = ("stale_task", "finding_evidence", "protocol_churn", "storage", "budget", "no_reference")
LABEL = {("submit",): "Send", ("kernel", "push"): "Push", ("dataset", "push"): "Push", ("gc",): "Delete",
         ("task", "merge"): "Merge"}


def act(label: str, argv: list[str], **kw) -> dict:
    return {"label": label, "argv": argv, **{k: v for k, v in kw.items() if v}}


def decorate(store: Store, items: list[dict]) -> list[dict]:
    for x in items:
        k = x["kind"]
        x["who"] = "chore" if k in CHORES else "advice" if k in ADVICE else "decision"
        x["actions"] = _actions(store, x)
    return items


def _task(store: Store, ref: str | None) -> dict | None:
    try:
        return tasks.get(store, ref) if ref else None
    except KxError:
        return None


def _actions(store: Store, x: dict) -> list[dict]:
    k, ref = x["kind"], x.get("ref")
    if k == "dead_run":
        return [act("Close dead runs", ["reap"])]
    if k == "remote_run":
        return [act("Copy it back", ["remote", "sync", ref])]
    if k in ("uncertain_submission", "sync"):
        return [act("Sync with Kaggle", ["sync"])]
    if k == "unmerged_task":
        return [act("Merge", ["task", "merge", ref], primary=True)]
    if k == "no_reference":
        return [act(f"Use {x['protocol']}", ["protocol", "use", x["protocol"]])] if x.get("protocol") else []
    if k == "resume_task":
        who = decide.resumer(store, ref)
        return [act(f"Resume with {who}", ["spawn", ref, "-w", who], primary=True)] if who else []
    if k == "worker_failed":
        t = _task(store, x.get("task"))
        row = store.one("SELECT agent FROM workers WHERE id=?", (ref,))
        who = row["agent"] if row else None
        if t and t["status"] in ("open", "waiting") and who and decide.resumer(store, t["id"]) == who:
            return [act(f"Retry with {who}", ["spawn", t["id"], "-w", who])]
        return []
    if k == "lost_claim":  # another machine has the task: the worker here works for nothing
        return [act("Stop the worker", ["stop", x["fix"].split()[-1]])] if x["fix"].startswith("kx stop ") else []
    if k == "task_waits_on_you":
        t = _task(store, ref)
        if t is None:
            return []
        who = decide.resumer(store, t["id"])
        resume = ["spawn", ref, "-w", who] if who else None
        argv = decide.command(t["waiting_on"])
        if argv:
            return [act(LABEL[decide.verb(argv)], ["approve", ref, "--yes"], preview=["approve", ref], primary=True,
                        resume=resume),
                    act("Decline", ["answer", ref], input="Why not? The agent that resumes the task reads it",
                        resume=resume)]
        return [act("Answer", ["answer", ref], input="Your answer", primary=True, resume=resume)]
    return []


# -- what the dashboard and the panel may run ----------------------------------------------------------------------

ID = r"\d{1,9}(?:-[a-z]+(?:-[a-z]+)*)?(?:~\w{1,12})?"  # T4, T4-quiet-otter, T4-quiet-otter~3f2a
T = re.compile(r"T" + ID)
RUN = re.compile(r"r?" + ID)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,99}")
TEXT_MAX = 4000


def _text(s: str) -> bool:
    return bool(s.strip()) and len(s) <= TEXT_MAX


def allowed(argv) -> bool:
    """A command line the dashboard and the panel may run for the person: settling what needs them, notes and tasks,
    starting or stopping a worker, and kx's own bookkeeping. Nothing else (no exec, no --yes but on approve)."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return False
    match argv:
        case ["approve", t] | ["approve", t, "--yes"] | ["task", "merge", t]:
            return bool(T.fullmatch(t))
        case ["answer", t, text] | ["task", "note", t, text]:
            return bool(T.fullmatch(t)) and _text(text)
        case ["note", r, text]:
            return bool(RUN.fullmatch(r)) and _text(text)
        case ["spawn", t, "-w", w]:
            return bool(T.fullmatch(t) and NAME.fullmatch(w))
        case ["task", "add", title, *rest]:
            return 0 < len(title.strip()) <= 300 and _task_add_options(rest)
        case ["stop", w] | ["protocol", "use", w]:
            return bool(NAME.fullmatch(w))
        case ["remote", "sync", r]:
            return bool(RUN.fullmatch(r))
        case ["reap"] | ["sync"] | ["remote", "sync"] | ["brief", "--mark"]:
            return True
    return False


def _task_add_options(rest: list[str]) -> bool:
    while rest:
        match rest:
            case ["--body", body, *more] if len(body) <= TEXT_MAX:
                rest = more
            case ["-p", n, *more] if re.fullmatch(r"-?\d{1,3}", n):
                rest = more
            case _:
                return False
    return True


def _safe(argv: list[str]) -> list[str]:
    """Text arguments as argparse cannot take them for options: `--` before a trailing text, --body=TEXT."""
    match argv:
        case ["answer" | "note" as v, ref, text]:
            return [v, ref, "--", text]
        case ["task", "note", ref, text]:
            return ["task", "note", ref, "--", text]
        case ["task", "add", title, *rest]:
            opts, i = [], 0
            while i < len(rest):
                opts += [f"--body={rest[i + 1]}"] if rest[i] == "--body" else [rest[i], rest[i + 1]]
                i += 2
            return ["task", "add", *opts, "--", title]
    return argv


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def execute(argv: list[str]) -> dict:
    """Run an allowed command line in-process, as the person (in the competition the caller is in): {ok, code, text,
    data}, or a preview {ok, code: 3, preview, problems, text, fix} when it only showed what it would do."""
    from kx.cli.main import call
    if not allowed(argv):
        raise KxError(f"not something the dashboard runs: kx {' '.join(argv) if isinstance(argv, list) else argv!r}")
    try:
        out = call(_safe(argv))
    except KxError as e:
        if e.code == 3 and isinstance(e.data, dict) and e.data.get("preview") is not None:
            return {"ok": True, "code": 3, "preview": e.data["preview"], "problems": e.data.get("problems") or [],
                    "text": str(e), "fix": e.fix}
        raise
    if out is None:
        return {"ok": True, "code": 0, "text": "", "data": None}
    return {"ok": True, "code": out.code, "text": ANSI.sub("", out.text or ""), "data": out.data}
