"""Who is calling: you (the human, or an agent you are talking to) or a background worker (KX_AGENT)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from kx.ledger.config import KxError
from kx.ledger.store import author


@dataclass(frozen=True)
class Actor:
    name: str
    worker: bool        # KX_AGENT is set: a background agent; it cannot send to Kaggle


def current() -> Actor:
    return Actor(author(), bool(os.environ.get("KX_AGENT")))


def not_worker(action: str, fix: str | None = None):
    """Refuse an action reserved to the person driving (merging into main, starting workers, ...)."""
    who = current()
    if who.worker:
        raise KxError(f"{action} is for the person driving, not for worker {who.name}",
                      fix=fix or "say what is needed in your task outcome (kx task done/wait) and end the session")
    return who
