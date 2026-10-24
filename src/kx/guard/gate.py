"""What leaves the machine (submissions, kernel and dataset pushes) is previewed first: you see the preview and rerun
with --yes. A worker cannot send: it records the command for its person (kx task wait) and ends; kx status shows the
waiting task with the command, and the person runs it."""

from __future__ import annotations

import shlex

from kx.ledger.config import KxError

STRIP = ("--yes", "-y", "--json")


def gate(actor, argv: list[str], summary: str, preview: dict, *, yes: bool, task: int | None = None):
    """Return when the action may go ahead; otherwise raise KxError(code=3) with the preview."""
    command = "kx " + " ".join(shlex.quote(a) for a in argv if a not in STRIP) + " --yes"
    if actor.worker:
        wait = f'kx task wait T{task} --on {shlex.quote(command)}' if task else "your task's outcome"
        raise KxError(f"workers do not send to Kaggle: {summary}", code=3,
                      fix=f"leave it to your person: record it with `{wait}` and end your session",
                      data={"preview": preview, "command": command})
    if not yes:
        raise KxError(f"not sent (preview): {summary}", code=3, fix=command, data={"preview": preview, "command": command})
