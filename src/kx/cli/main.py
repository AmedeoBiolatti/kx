"""kx command line. Every command takes --json; exit codes: 0 ok, 1 error, 2 usage, 3 needs confirmation,
4 over a budget or a Kaggle limit, 5 not comparable, 6 what kx wait waited for can no longer happen. The commands
live in one module per area (kx.cli.runs, ...)."""

from __future__ import annotations

import argparse
import sys

from kx.cli import board, common, guard, kaggle, remote, runs, setup, views, workers
from kx.ledger.config import KxError
from kx.views.text import emit_json, kv

AREAS = (views, runs, remote, board, kaggle, guard, workers, setup)  # the order commands appear in `kx --help`


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="one JSON document on stdout")
    p = argparse.ArgumentParser(prog="kx", parents=[common],
                                description="kx: the ledger and Kaggle interface of a competition. Start with `kx status`.",
                                epilog="exit codes: 0 ok · 1 error · 2 usage · 3 needs confirmation · 4 over budget/limit · 5 not comparable · 6 kx wait: failed or stalled instead")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(subs, name, help_, **kw):
        sp = subs.add_parser(name, parents=[common], help=help_, description=help_, **kw)
        if help_ is argparse.SUPPRESS:  # internal (worker-run, remote watch): argparse < 3.14 would list it anyway
            subs._choices_actions = [a for a in subs._choices_actions if a.dest != name]
        return sp

    def cmd(name, fn, help_, **kw):
        sp = add(sub, name, help_, **kw)
        sp.set_defaults(func=fn)
        return sp

    def group(name, help_):
        sp = add(sub, name, help_)
        return sp.add_subparsers(dest="action", metavar="ACTION", required=True)

    def action(g, name, fn, help_):
        sp = add(g, name, help_)
        sp.set_defaults(func=fn)
        return sp

    for area in AREAS:
        area.register(cmd, group, action)
    return p


def _print_error(e: KxError, as_json: bool):
    if as_json:
        emit_json({"ok": False, "error": str(e), "fix": e.fix, "code": e.code, **(e.data or {})})
        return
    data = e.data or {}
    if data.get("preview"):
        print(kv(data["preview"]))
        if data.get("problems"):
            print("problems:\n" + "\n".join(f"  - {x}" for x in data["problems"]))
        print(f"kx: {e}")
    else:
        print(f"kx: {e}", file=sys.stderr)
    if e.fix:
        print(f"  fix: {e.fix}", file=sys.stderr if not data.get("preview") else sys.stdout)


def main(argv: list[str] | None = None):
    common.ARGV[:] = list(sys.argv[1:] if argv is None else argv)
    p = build_parser()
    a = p.parse_args(common.ARGV)
    if not getattr(a, "func", None):
        p.print_help()
        return 0
    try:
        out = a.func(a)
    except KxError as e:
        _print_error(e, a.json)
        return e.code
    except KeyboardInterrupt:
        print("kx: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    if out is None:
        return 0
    if a.json:
        emit_json(out.data if out.data is not None else {"ok": True})
    elif out.text:
        print(out.text)
    return out.code


def entry():
    sys.exit(main())
