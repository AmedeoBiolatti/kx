"""What waits on the person, and their answer.

A task waits on the person when a worker parks it on a kx command it may not run itself (`kx task wait T7 --on
"kx submit r41 ... --yes"`) or on a question for them. The person settles it in one step: `kx approve T7` runs the
command as them (without --yes it shows the command's own preview), `kx answer T7 TEXT` replies (to a command: it is
not run, and why). Running the command by hand settles it too. Either way the task can go on: it waits on what
follows (`submission:S12-brave-lynx`, scored on Kaggle) or on nothing (`answered: ...`), and kx status says so, with the worker
that can resume it.
"""

from __future__ import annotations

import shlex

from kx.board import tasks
from kx.ledger.config import KxError
from kx.ledger.store import Store, author, now

ANSWERED = "answered:"  # waiting_on of a task the person settled: what follows is why it can go on
# the commands a task may wait on that the person approves with `kx approve` (the verb's words)
APPROVABLE = (("submit",), ("kernel", "push"), ("dataset", "push"), ("gc",), ("task", "merge"))
STRIP = ("--yes", "-y", "--json")


def command(on: str | None) -> list[str] | None:
    """The kx command a task waits on, as argv without `kx`, --yes and --json, when it is one `kx approve` runs."""
    if not on or not on.startswith("kx "):
        return None
    try:
        argv = [a for a in shlex.split(on)[1:] if a not in STRIP]
    except ValueError:
        return None
    return argv if verb(argv) else None


def verb(argv: list[str]) -> tuple[str, ...] | None:
    return next((v for v in APPROVABLE if tuple(argv[:len(v)]) == v and len(argv) > len(v)), None)


def describe(argv: list[str], asker: str | None = None) -> str:
    """What a command asks the person, in words: "sonnet-1 asks to submit r0003 · why: ... · expects: ..."."""
    words = verb(argv) or tuple(argv[:1])
    rest = argv[len(words):]
    opts, pos, i = {}, [], 0
    while i < len(rest):
        if rest[i].startswith("-") and i + 1 < len(rest) and not rest[i + 1].startswith("-"):
            opts[rest[i]], i = rest[i + 1], i + 2
        else:
            if not rest[i].startswith("-"):
                pos.append(rest[i])
            i += 1
    what = {("submit",): f"submit {' '.join(pos[:1])}", ("kernel", "push"): f"push the kernel of {' '.join(pos[:1])}",
            ("dataset", "push"): f"push {' '.join(pos[1:2])} of {' '.join(pos[:1])} as a Kaggle dataset",
            ("gc",): f"delete what {' '.join(pos) or 'discarded and failed runs'} made",
            ("task", "merge"): f"merge {' '.join(pos[:1])}'s branch into the main checkout"}.get(words, " ".join(argv))
    extra = [f"{k}: {opts[o]}" for o, k in (("--why", "why"), ("--expect", "expects")) if opts.get(o)]
    return f"{asker or 'an agent'} asks to {what}" + "".join(f" · {e}" for e in extra)


def asks_person(t: dict) -> bool:
    """A waiting task that waits on the person: a kx command, or words for a person."""
    on = t.get("waiting_on") or ""
    return t.get("status") == "waiting" and not on.startswith(ANSWERED) and (
        on.startswith("kx ") or any(w in on.lower() for w in ("human", "you", "manual", "person")))


def resumer(store: Store, task_id: str) -> str | None:
    """The worker that last worked on a task, when comp.yaml still has it and it is not running: who resumes it."""
    row = store.one("SELECT agent FROM workers WHERE task=? ORDER BY started DESC LIMIT 1", (task_id,))
    if row is None or row["agent"] not in store.comp.workers:
        return None
    if store.one("SELECT 1 FROM workers WHERE agent=? AND finished IS NULL", (row["agent"],)):
        return None
    return row["agent"]


def settle(store: Store, words: tuple[str, ...], run_id: str, outcome: str, then: str | None = None) -> list[str]:
    """The person did what waiting tasks asked of them (`words` on run_id, e.g. ("submit",) on r41): each task now
    waits on `then` (what follows: submission:S12-brave-lynx) or can go on, with a note saying what was done."""
    out = []
    for t in store.all("SELECT * FROM tasks WHERE status='waiting' AND waiting_on LIKE 'kx %'"):
        argv = command(t["waiting_on"])
        if not argv or verb(argv) != words:
            continue
        try:
            if store.resolve(argv[len(words)]) != run_id:
                continue
        except KxError:
            continue
        mark(store, t["id"], f"{outcome} (`{t['waiting_on']}`, which this task waited on)", then or f"{ANSWERED} {outcome}")
        out.append(t["id"])
    return out


def mark(store: Store, task_id: str, note: str, waiting_on: str):
    t = tasks.get(store, task_id)
    with store.transaction():
        tasks.note(store, t["id"], note)
        store.update("tasks", {"waiting_on": waiting_on, "updated": now()}, id=t["id"], **tasks.held(t))


def answer(store: Store, ref, text: str) -> dict:
    """The person's reply to what a waiting task waits on. To a command: it is not run, and the text says why."""
    t = tasks.get(store, ref)
    text = " ".join(text.split())
    if not text:
        raise KxError("an answer needs words: the agent that resumes the task reads them")
    if t["status"] != "waiting":
        raise KxError(f"{t['id']} is {t['status']}, not waiting on anything", fix=f"kx task note {t['id']} TEXT")
    on, who = t["waiting_on"] or "", author()
    if on.startswith("kx "):
        note, why = f"not run: `{on}`. {text}", f"{who} declined: {text}"
    else:
        note, why = f"answer to “{on}”: {text}", f"{who} replied: {text}"
    mark(store, t["id"], note, f"{ANSWERED} {why[:300]}")
    return tasks.get(store, t["id"])


def resolved(on: str) -> str | None:
    """Why a task settled by the person can go on (its waiting_on starts with answered:), or None."""
    return (on[len(ANSWERED):].strip() or "answered") if on.startswith(ANSWERED) else None


def waits(t: dict) -> str | None:
    """What a task's wait says to a reader: "waits on run:r12", "can go on: bamedeo replied: ...", or None."""
    if t.get("status") != "waiting":
        return None
    on = t.get("waiting_on") or ""
    return f"can go on: {why}" if (why := resolved(on)) is not None else f"waits on {on}"
