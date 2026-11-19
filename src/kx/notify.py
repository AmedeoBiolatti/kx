"""Telling the person when something needs them, so nothing waits unseen (v1 stalled for a week on approvals nobody
saw). kx sends a notice when it records something: a worker ending (with its request or its failure), a run ending
(a new best), a Kaggle sync (a submission scored), a task parked on the person. Never on a timer, each thing once,
several at once in one notice.

Where notices go, per machine (`~/.config/kx/machine.yaml`):

    notify:
      desktop: true                    # default: on where notify-send (Linux, with a display) or macOS can show one
      ntfy: https://ntfy.sh/TOPIC      # a phone with the ntfy app subscribed to TOPIC (a long random one: anyone
                                       # who knows it reads the notices), or your own ntfy server
      command: [my-notifier, --flag]   # anything else: run with the title and the text as two more arguments
      link: http://localhost:8765      # what a notice opens (the dashboard)

`notify: false` or KX_NOTIFY=off turns them off. `kx notify --test` sends one through each.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import timedelta
from urllib.parse import quote, urlsplit

from kx.ledger.store import Store, age_s, now, parse_ts

KINDS = ("task_waits_on_you", "resume_task", "worker_failed", "failed_submission", "unmerged_task")
KEEP_DAYS = 14   # a notice's key is kept this long: the same thing is not announced twice
FRESH_S = 86400  # news older than this when first seen (a ledger kx never notified from) is not announced
MAX_LINES = 6
TIMEOUT_S = 5


def config() -> dict:
    """The channels of this machine: {desktop: bool, ntfy: url, command: [...], link: url}; {} when off."""
    if (os.environ.get("KX_NOTIFY") or "").lower() in ("off", "0", "false", "no"):
        return {}
    from kx import sandbox
    try:
        c = sandbox.machine().get("notify", {})
    except Exception:  # noqa: BLE001 - a broken machine.yaml says so elsewhere; notices just stay off
        return {}
    if c is False:
        return {}
    c = dict(c) if isinstance(c, dict) else {}
    if "desktop" not in c:
        c["desktop"] = desktop_available()
    if isinstance(c.get("command"), str):
        c["command"] = [c["command"]]
    c.setdefault("link", "http://localhost:8765")
    return c if c.get("desktop") or c.get("ntfy") or c.get("command") else {}


def desktop_available() -> bool:
    if sys.platform == "darwin":
        return bool(shutil.which("osascript"))
    return bool(shutil.which("notify-send") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))


def _key(*parts) -> str:
    return ":".join(str(p) for p in parts[:-1]) + ":" + hashlib.sha1(str(parts[-1]).encode()).hexdigest()[:10]


def news(store: Store) -> list[tuple[str, str, bool]]:
    """What a notice would say now: [(key, line, needs the person)]."""
    from kx.runs.protocol import ranking, reference
    from kx.views.status import needs_you
    out = [(_key(x["kind"], x.get("ref"), x["what"]), x["what"], True) for x in needs_you(store) if x["kind"] in KINDS]
    ref = reference(store)
    if ref["id"]:
        board = ranking(store, ref["id"], 2)
        if board and (age_s(board[0]["finished"]) or FRESH_S + 1) <= FRESH_S:
            b = board[0]
            was = f" (was {board[1]['value']:.6g}, {board[1]['id']})" if len(board) > 1 else ""
            out.append((f"best:{ref['id']}:{b['id']}", f"{b['id']} is the new best under {ref['id']}: "
                                                        f"{b['value']:.6g}{was}", False))
    for s in store.all("SELECT s.*, (SELECT MIN(ts) FROM submission_scores x WHERE x.submission_id=s.id) scored "
                       "FROM submissions s WHERE s.public IS NOT NULL ORDER BY s.n, s.id"):
        if (age_s(s["scored"]) or FRESH_S + 1) <= FRESH_S:
            out.append((f"scored:{s['id']}", s["id"] + (f" ({s['run_id']})" if s["run_id"] else "")
                        + f" scored {s['public']:.6g} on Kaggle", False))
    workers = set(store.comp.workers)
    for t in store.all("SELECT * FROM tasks WHERE status='done' AND owner IS NOT NULL ORDER BY n, id"):
        who = t["owner"].split("@")[0]  # codex-1@laptop
        if who in workers and (age_s(t["updated"]) or FRESH_S + 1) <= FRESH_S:
            out.append((f"done:{t['id']}", f"{who} finished {t['id']} ({t['result']}): "
                                             f"{(t['outcome'] or t['title'])[:160]}", False))
    return out


def check(store: Store, send=None) -> list[str]:
    """Send what is new since the last notice, once; the lines sent ([] when nothing, or notices are off)."""
    c = config() if send is None else {"send": send}
    if not c:
        return []
    sent = store.setting("notified") or {}
    fresh = [(k, line, need) for k, line, need in news(store) if k not in sent]
    if not fresh:
        return []
    n = sum(need for _, _, need in fresh)
    title = f"kx · {store.comp.slug}" + (f": {n} thing{'s' if n != 1 else ''} need{'s' if n == 1 else ''} you" if n else "")
    lines = [line for _, line, _ in sorted(fresh, key=lambda x: not x[2])]
    body = "\n".join(lines[:MAX_LINES]) + (f"\n… {len(lines) - MAX_LINES} more: kx" if len(lines) > MAX_LINES else "")
    link = f"{c.get('link', '').rstrip('/')}/?comp={quote(page_name(store.comp.main_root), safe='')}#status" \
        if c.get("link") else None
    ok = send(title, body, link) if send is not None else deliver(c, title, body, link)
    if ok:
        stamp = now()
        cutoff = (parse_ts(stamp) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        sent = {k: v for k, v in sent.items() if v >= cutoff}
        sent.update({k: stamp for k, _, _ in fresh})
        store.set_setting("notified", sent)
    return lines if ok else []


def page_name(root) -> str:
    """The competition's name in the dashboard's picker (its folder's, with its parent's when two share one)."""
    try:
        from kx.views.dashboard import competitions
        comps, _ = competitions()
        return next((k for k, v in comps.items() if v.resolve() == root.resolve()), root.name)
    except Exception:  # noqa: BLE001 - a link to the page as it opens is still a link
        return root.name


def quietly(store: Store) -> None:
    """check, where kx records something: a notice that cannot be sent never gets in the way."""
    try:
        if config():
            check(store)
    except Exception as e:  # noqa: BLE001
        print(f"kx: note: no notice sent ({type(e).__name__}: {e})", file=sys.stderr)


def deliver(c: dict, title: str, body: str, link: str | None) -> bool:
    """Through every channel configured; True when at least one took it."""
    ok = False
    if c.get("desktop"):
        ok |= desktop(title, body)
    if c.get("ntfy"):
        ok |= ntfy(c["ntfy"], title, body, link)
    if c.get("command"):
        ok |= command(c["command"], title, body, link)
    return ok


def _run(argv: list[str], env: dict | None = None) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=TIMEOUT_S, env=env).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def desktop(title: str, body: str) -> bool:
    if sys.platform == "darwin":
        return _run(["osascript", "-e", f"display notification {json.dumps(body)} with title {json.dumps(title)}"])
    return _run(["notify-send", "--app-name=kx", title, body])


def ntfy(url: str, title: str, body: str, link: str | None) -> bool:
    """ntfy's JSON publishing (UTF-8 throughout): POST {topic, title, message, click} to the server's root."""
    u = urlsplit(url)
    topic = u.path.rstrip("/").rsplit("/", 1)[-1]
    if u.scheme not in ("http", "https") or not topic:
        print(f"kx: note: notify.ntfy must be a URL ending in the topic, e.g. https://ntfy.sh/TOPIC (not {url!r})",
              file=sys.stderr)
        return False
    root = f"{u.scheme}://{u.netloc}{u.path.rstrip('/').rsplit('/', 1)[0]}/"
    msg = {"topic": topic, "title": title, "message": body, "tags": ["kx"]}
    if link and not link.startswith(("http://localhost", "http://127.0.0.1")):  # a phone cannot open this machine's
        msg["click"] = link
    req = urllib.request.Request(root, data=json.dumps(msg).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return 200 <= r.status < 300
    except OSError:
        return False


def command(argv: list[str], title: str, body: str, link: str | None) -> bool:
    return _run([*argv, title, body], env={**os.environ, "KX_NOTIFY_LINK": link or ""})
