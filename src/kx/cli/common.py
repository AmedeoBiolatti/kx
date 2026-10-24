"""What every command module shares: the Out result, the store opener and the command line being run."""

from __future__ import annotations

from pathlib import Path

from kx.board import tasks
from kx.ledger import config
from kx.ledger.store import Store

ARGV: list[str] = []  # the command line being run, set by kx.cli.main.main


class Out:
    """What a command returns: data for --json and the text shown otherwise."""

    def __init__(self, data=None, text: str | None = None, code: int = 0):
        self.data, self.text, self.code = data, text, code


def S() -> Store:
    return Store(config.load(), auto_backup=True)



def argv() -> list[str]:
    """The command line being run (to file it as a request), without --json."""
    return [a for a in ARGV if a != "--json"]


def task_here() -> int | None:
    return tasks.current(Path.cwd())
