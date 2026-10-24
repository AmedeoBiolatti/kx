"""Commands: remote list, add, check, setup, sync, follow, kill (runs on other machines; started with
kx exec --on HOST)."""

from __future__ import annotations

import sys

from kx.cli.common import Out, S
from kx.ledger import config
from kx.ledger.config import KxError
from kx.ledger.store import age_s, rid
from kx.views.text import ago, dim, dur, table


def _comp():
    try:
        return config.load()
    except KxError:
        return None  # outside a competition: machine-wide hosts only


def cmd_list(a):
    from kx.remote import execute, hosts
    comp = _comp()
    hs = hosts.load(comp)
    runs = []
    if comp is not None:
        with S() as st:
            for r in execute.running(st):
                inf = execute.info(st, r["id"])
                runs.append({"run": rid(r["id"]), "host": inf["host"], "hypothesis": r["hypothesis"],
                             "elapsed_s": age_s(r["created"]), "synced": inf.get("synced")})
    data = {"hosts": [{"name": h.name, "kind": h.kind, "ssh": h.ssh, "mailbox": h.mailbox, "local": h.local, "dir": h.dir, "gpus": h.gpus,
                       "usd_per_hour": h.usd_per_hour, "running": sum(x["host"] == h.name for x in runs)} for h in hs.values()],
            "running": runs, "hosts_file": str(hosts.HOSTS_FILE)}
    if not hs:
        return Out(data, f"no hosts configured. Add them to {hosts.HOSTS_FILE}:\n"
                         "  lambda: {ssh: ubuntu@203.0.113.7, gpus: 1, usd_per_hour: 1.29}\n"
                         "then: kx remote check lambda")
    text = table([[h["name"], h["kind"], h["ssh"] or h["mailbox"] or "local", h["gpus"] or "", f"${h['usd_per_hour']:g}/h" if h["usd_per_hour"] else "",
                   h["running"] or ""] for h in data["hosts"]], ["host", "kind", "where", "gpus", "price", "running"])
    if runs:
        text += "\n\n" + table([[x["run"], x["host"], dur(x["elapsed_s"]), ago(x["synced"]) + " ago" if x["synced"] else "never",
                                 x["hypothesis"] or ""] for x in runs], ["run", "host", "elapsed", "synced", "hypothesis"])
    return Out(data, text)


def cmd_check(a):
    from kx.remote import hosts
    h = hosts.get(a.host, _comp())
    checks = hosts.check(h)
    ok = all(c["ok"] for c in checks)
    lines = [("ok   " if c["ok"] else "FAIL ") + c["check"] + ("" if c["ok"] or not c["fix"] else dim(f"  fix: {c['fix']}"))
             for c in checks]
    return Out({"host": h.name, "ok": ok, "checks": checks}, "\n".join(lines), 0 if ok else 1)


WSL_STEPS = ("kx runs need a Linux shell there. On {name}: install WSL2 (`wsl --install` in an admin PowerShell), "
             "then inside it install Tailscale and run `sudo tailscale up --ssh --hostname {name}-wsl`; then here: "
             "kx remote add {name}-wsl. To keep it up with no terminal open, put `vmIdleTimeout=-1` under [wsl2] in "
             "%UserProfile%\\.wslconfig on Windows")


def _reach(h) -> str | None:
    """Why ssh to the host fails, or None. Tailscale SSH in `check` mode waits for a browser login: kx cannot."""
    import subprocess
    try:
        p = subprocess.run([*h.transport.ssh_argv, "echo kx-ok"], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        if "tailscale" in err.lower():
            return ("Tailscale SSH asks for a browser login (`check` mode), which kx cannot do: in the Tailscale admin "
                    "console's access controls, set the ssh rule for your own devices to \"action\": \"accept\"")
        return "ssh timed out after 30 s"
    if p.returncode == 0 and "kx-ok" in p.stdout:
        return None
    err = (p.stderr or p.stdout).strip()[-400:]
    if "tailscale" in err.lower() and "check" in err.lower():
        return f"{err}\n(set the Tailscale ssh rule for your own devices to \"accept\")"
    if "host key verification failed" in err.lower():
        return (f"{err}\n(ssh does not know {h.ssh.split('@')[-1]}'s host key and kx cannot ask: connect once with "
                f"`ssh {h.ssh}` and accept it; machines added from Tailscale get theirs from it)")
    hint = f"is `tailscale up --ssh` running on it, and does the user {h.ssh.split('@')[0]} exist there (--user)?"
    return f"{err or f'ssh exit {p.returncode}'}\n({hint})"


def cmd_add(a):
    import getpass

    from kx.remote import execute, hosts
    comp = _comp()
    if a.name in hosts.load(comp) and not a.force:
        raise KxError(f"there is a host {a.name!r} already", fix=f"kx remote check {a.name} (or --force to add it again)")
    peer = None
    if a.ssh:
        ssh = a.ssh
    else:
        peer = hosts.find_peer(a.name, hosts.tailscale_peers())
        if peer["os"] == "windows":
            raise KxError(f"{a.name} runs Windows", fix=WSL_STEPS.format(name=a.name))
        if not peer["online"]:
            raise KxError(f"{a.name} is offline on Tailscale", fix=f"wake {a.name}, or start Tailscale on it")
        ssh = f"{a.user or getpass.getuser()}@{peer['dns'] or peer['ip']}"
        hosts.trust([peer["dns"], peer["ip"]], peer.get("host_keys") or [])
    entry = {"ssh": ssh, **({"python": a.python} if a.python else {})}
    h = hosts.Host(name=a.name, **entry)
    why = _reach(h)
    if why:
        raise KxError(f"cannot reach {a.name} as {ssh}: {why}", fix=f"fix that, then: kx remote add {a.name}"
                      + ("" if peer else f" --ssh {ssh}"))
    p = h.transport.run("nvidia-smi -L 2>/dev/null | grep -c '^GPU' || true; uname -sm", check=False, timeout=60)
    out = p.stdout.split("\n")
    gpus = int(out[0]) if out and out[0].strip().isdigit() else 0
    if gpus:
        entry["gpus"] = h.gpus = gpus
    path = hosts.add(a.name, entry)
    print(f"kx: added {a.name} ({out[1] if len(out) > 1 else '?'}, {gpus} GPU(s)) to {path}", file=sys.stderr)
    r = {"host": a.name, "entry": entry, "hosts_file": str(path), "setup": None}
    if comp is not None and not a.no_setup:
        r["setup"] = execute.setup(a.name)
    nxt = f"kx exec --on {a.name} -- ..." if r["setup"] else f"kx remote setup {a.name} (in a competition)"
    return Out(r, f"{a.name} added ({ssh}{f', {gpus} GPU(s)' if gpus else ''})" + ("; ready for runs" if r["setup"] else "")
                  + f"\nnext: {nxt}")


def cmd_setup(a):
    from kx.remote import execute
    r = execute.setup(a.host)
    env = r["env"]
    if r.get("start"):  # a mailbox host: its agent sets each run up itself
        return Out(r, f"the mailbox has kx and data {r['data_version'] or 'none'} for {r['host']}. Start its agent there "
                      f"(it needs rclone with the same remote, and python3):\n  {r['start']}\n"
                      f"keep it running (nohup ... &, tmux, or a notebook cell); then: kx remote check {r['host']}")
    return Out(r, f"{r['host']} is ready for runs: data {r['data_version'] or 'none'}"
                  + (f", environment {env['key']} (Python {env['python']}, torch {env['torch'] or 'none'}"
                     f"{', CUDA' if env['cuda'] else ''})" if env else ""))


def cmd_sync(a):
    from kx.remote import execute
    items = execute.sync(a.run)
    if not items:
        return Out([], "no remote runs running")
    return Out(items, "\n".join(f"{x['run']} on {x['host']}: {x['state']}" + (f" (exit {x['exit']})" if "exit" in x else "")
                                + (" · files over 32 MB still on the host (low disk space here)" if x.get("complete") is False else "")
                                + (f" · copy failed: {x['error']}" if x.get("error") else "") for x in items))


def cmd_follow(a):
    from kx.remote import execute
    with S() as st:
        run_id = st.resolve(a.run)
    code = execute.follow(run_id, echo=not a.json, from_start=False)
    return Out({"run": rid(run_id), "exit": code}, None, code)


def cmd_kill(a):
    from kx.remote import execute
    r = execute.kill(a.run)
    return Out(r, f"killed {r['run']} on {r['host']}")


def register(cmd, group, action):
    g = group("remote", "runs on other machines (start one with kx exec --on HOST)")
    action(g, "list", cmd_list, "configured hosts and the remote runs in progress")
    s = action(g, "add", cmd_add, "add a machine on your Tailscale network (or --ssh USER@HOST), check it, set it up")
    s.add_argument("name", help="its Tailscale name (a unique prefix will do); the host's name in kx")
    s.add_argument("--ssh", help="USER@HOST to reach it, instead of finding it on Tailscale")
    s.add_argument("--user", help="the user on it (default: yours here)")
    s.add_argument("--python", help="an interpreter there with kx's and the project's packages (skips uv)")
    s.add_argument("--no-setup", action="store_true", help="only add it")
    s.add_argument("--force", action="store_true", help="add it although a host has that name")
    s = action(g, "check", cmd_check, "can HOST run kx runs? (reach, tools, bubblewrap, disk, GPU; a mailbox's agent)")
    s.add_argument("host")
    s = action(g, "setup", cmd_setup, "get HOST ready before the first run: kx, the data, the environment")
    s.add_argument("host")
    s = action(g, "sync", cmd_sync, "copy remote runs back and record the ones that ended (one ended run: all its files)")
    s.add_argument("run", nargs="?")
    s = action(g, "follow", cmd_follow, "stream a remote run's output until it ends (Ctrl-C stops following only)")
    s.add_argument("run")
    s = action(g, "kill", cmd_kill, "stop a remote run and record it as failed")
    s.add_argument("run")
