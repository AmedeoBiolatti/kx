"""`kx dashboard`: a local web page over the same data as the CLI's --json output.

Each API call runs the matching kx command in-process (so the page and the terminal never disagree). The page only
reads, except `kx sync` (read-only on Kaggle), which needs the per-process token embedded in the page.
"""

from __future__ import annotations

import io
import json
import math
import os
import secrets
import subprocess
import sys
import threading
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kx.ledger import config
from kx.ledger.config import KxError

TEMPLATE = Path(__file__).parent / "dashboard.html"
LOCK = threading.Lock()
SYNCING = threading.Lock()  # one kx sync at a time; it runs apart from LOCK, so the page keeps answering meanwhile

ROUTES = {  # path -> kx argv builder from the query string
    "status": lambda q: ["status"],
    "runs": lambda q: ["runs", "-n", q.get("n", "200")] + (["--task", q["task"]] if q.get("task") else [])
    + (["--tag", q["tag"]] if q.get("tag") else []) + (["--curves"] if q.get("curves") else [])
    + (["--config"] if q.get("config") else []),
    "compare": lambda q: ["compare", need(q, "a"), need(q, "b")],
    "board": lambda q: ["board", "-n", q.get("n", "50")] + ([q["protocol"]] if q.get("protocol") else []),
    "protocols": lambda q: ["protocol", "list"],
    "tasks": lambda q: ["task", "list"] + (["--all"] if q.get("all") else []),
    "findings": lambda q: ["finding", "list"] + (["--all"] if q.get("all") else []) + (["--tag", q["tag"]] if q.get("tag") else []),
    "subs": lambda q: ["subs", "-n", q.get("n", "30")],
    "workers": lambda q: ["workers", "--all"],
    "budget": lambda q: ["budget"],
}


def need(q: dict, key: str) -> str:
    if not q.get(key):
        raise KxError(f"missing ?{key}= in the request")
    return q[key]


TRIM = {  # route -> what the page reads of the command's data
    "protocols": lambda d: {**d, "protocols": [{k: v for k, v in p.items() if k != "spec"} for p in d["protocols"]]},
}
_parser = None


def run_kx(argv: list[str]):
    """Run a kx command in-process and return its data (what --json prints)."""
    global _parser
    if _parser is None:  # building it takes longer than most commands (callers hold LOCK)
        from kx.cli.main import build_parser
        _parser = build_parser()
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            a = _parser.parse_args(argv)
            out = a.func(a)
    except SystemExit:  # argparse refuses the arguments (a bad ?n=): an error page, not a dropped connection
        lines = [x for x in err.getvalue().strip().splitlines() if x.strip()]
        raise KxError(lines[-1] if lines else f"bad request: kx {' '.join(argv)}") from None
    return out.data if out is not None else None


def _finite(o):
    """JSON has no NaN or Infinity (the browser refuses the whole response): they become null."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


def to_json(obj) -> bytes:
    try:
        return json.dumps(obj, default=str, allow_nan=False).encode()
    except ValueError:  # a NaN somewhere (a metric, a config value): only then walk it
        return json.dumps(_finite(obj), default=str, allow_nan=False).encode()


def sync(root: Path) -> tuple[int, dict]:
    """kx sync in its own process: it takes minutes (rclone, games), during which the page goes on reading."""
    try:
        p = subprocess.run([sys.executable, "-m", "kx", "sync", "--json"], cwd=root, capture_output=True, text=True,
                           timeout=3600)
    except subprocess.TimeoutExpired:
        return 500, {"ok": False, "error": "kx sync took over an hour"}
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return 500, {"ok": False, "error": (p.stderr or p.stdout).strip()[-600:] or f"kx sync exited {p.returncode}"}
    return (200 if p.returncode == 0 else 400), data


def history(root: Path, protocol: str | None = None) -> dict:
    """Scores under `protocol` (default: the reference) in run order (discarded runs left out, as on the board), with
    the best so far and each run's reported numeric metrics
    (r.log / logged_metrics.json), for the score-over-time and metrics-over-time charts. `protocols` lists every
    protocol with a scored run, for the picker."""
    from kx.ledger.store import Store, loads
    from kx.runs.progress import number
    from kx.runs.protocol import reference
    with Store(config.load(root)) as st:
        ref = reference(st)
        live = "r.status='done' AND r.id NOT IN (SELECT run_id FROM tags WHERE tag='discarded')"
        protos = [{"id": r["id"], "n": r["n"]} for r in st.all(
            f"SELECT s.protocol_id id, COUNT(*) n FROM scores s JOIN runs r ON r.id=s.run_id WHERE {live} "
            "GROUP BY s.protocol_id ORDER BY (s.protocol_id=?) DESC, (s.protocol_id LIKE 'kaggle:%'), n DESC", (ref["id"] or "",))]
        pid = protocol if any(p["id"] == protocol for p in protos) else ref["id"]
        if not pid:
            return {"reference": ref, "protocol": None, "protocols": protos, "points": []}
        direction = st.protocol(pid)["direction"]
        rows = st.all("SELECT r.id, r.n, r.created, r.hypothesis, r.metrics, s.value, s.se FROM scores s JOIN runs r ON r.id=s.run_id "
                      f"WHERE s.protocol_id=? AND {live} ORDER BY r.n, r.id", (pid,))
        best, pts = None, []
        for r in rows:
            if best is None or (r["value"] > best if direction == "max" else r["value"] < best):
                best = r["value"]
            metrics = loads(r["metrics"]) or {}
            pts.append({"run": r["id"], "n": r["n"], "created": r["created"], "value": r["value"], "se": r["se"], "best": best,
                        "hypothesis": r["hypothesis"],
                        "metrics": {k: v for k, v in metrics.items() if number(v)} if isinstance(metrics, dict) else {}})
        return {"reference": ref, "protocol": pid, "protocols": protos, "direction": direction, "points": pts}


def leaderboard(root: Path, protocol: str | None = None) -> dict:
    """The leaderboard side of progress: our public scores per submission, leaderboard snapshots (our rank, top, medal
    lines) taken at each kx sync, the range each medal line can have had before the first (submit.lb_bounds), and local
    vs public score for every run with both, under `protocol` (default: the
    reference), with a least-squares line once there are two."""
    from kx.ledger.store import Store
    from kx.runs.protocol import KAGGLE_PUBLIC, reference
    with Store(config.load(root)) as st:
        ref = reference(st)
        subs = [{"id": r["id"], "created": r["created"], "public": r["public"], "private": r["private"],
                 "run": r["run_id"] if r["run_id"] else None, "message": r["message"]}
                for r in st.all("SELECT * FROM submissions WHERE public IS NOT NULL ORDER BY created, n")]
        snaps = [dict(r) for r in st.all("SELECT * FROM lb_snapshots ORDER BY ts")]
        bounds = st.setting("lb_bounds")
        protos = [{"id": r["id"], "n": r["n"]} for r in st.all(
            "SELECT s.protocol_id id, COUNT(*) n FROM scores s JOIN scores k ON k.run_id=s.run_id AND k.protocol_id=? "
            "WHERE s.protocol_id NOT LIKE 'kaggle:%' GROUP BY s.protocol_id ORDER BY (s.protocol_id=?) DESC, n DESC",
            (KAGGLE_PUBLIC, ref["id"] or ""))]
        pid = protocol or (ref["id"] if any(p["id"] == ref["id"] for p in protos) else protos[0]["id"] if protos else None)
        pairs = [{"run": r["id"], "hypothesis": r["hypothesis"], "local": r["local"], "se": r["se"],
                  "public": r["public"], "submissions": r["n"]}
                 for r in st.all("SELECT r.id, r.hypothesis, s.value local, s.se, k.value public, k.n FROM scores s "
                                 "JOIN scores k ON k.run_id=s.run_id AND k.protocol_id=? JOIN runs r ON r.id=s.run_id "
                                 "WHERE s.protocol_id=? ORDER BY r.n, r.id", (KAGGLE_PUBLIC, pid))] if pid else []
    fit = None
    if len(pairs) >= 2:
        xs, ys = [p["local"] for p in pairs], [p["public"] for p in pairs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        if sxx > 0:
            slope = sxy / sxx
            fit = {"slope": slope, "intercept": my - slope * mx, "r": sxy / (sxx * syy) ** 0.5 if syy > 0 else None}
    return {"reference": ref, "protocol": pid, "protocols": protos, "submissions": subs, "snapshots": snaps,
            "bounds": bounds, "pairs": pairs, "fit": fit}


def describe(name: str, root: Path) -> dict:
    """What the header shows for a competition: kind, mode, metric, days left, its Kaggle pages."""
    try:
        comp = config.load(root)
        from kx.views.status import _deadline
        from kx.ledger.store import Store
        from kx.kaggle import competition
        with Store(comp) as st:
            dl = _deadline(st)
            datasets = [r["target"] for r in st.all("SELECT target, MAX(created) last FROM pushes WHERE kind='dataset' "
                                                     "GROUP BY target ORDER BY last DESC")]
        return {"slug": name, "path": str(root), "title": comp.slug, "kind": comp.kind, "mode": comp.mode,
                "metric": comp.metric, "direction": comp.direction, "days_left": dl["days_left"],
                "links": competition.links(comp),
                "datasets": [{"ref": d, "url": competition.dataset_url(d)} for d in datasets]}
    except Exception as e:  # noqa: BLE001 - a broken competition still appears in the picker, with why
        return {"slug": name, "path": str(root), "error": str(e) if isinstance(e, KxError) else f"{type(e).__name__}: {e}",
                "fix": getattr(e, "fix", None)}


class Handler(BaseHTTPRequestHandler):
    server_version = "kx"
    timeout = 30  # an idle connection (a browser opens some ahead of time) is dropped, not kept forever
    token = ""
    comps: dict[str, Path] = {}
    default = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, to_json(obj))

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1", "[::1]")

    def _root(self, q: dict) -> Path:
        slug = q.get("comp") or self.default
        if slug not in self.comps:
            raise KxError(f"unknown competition {slug!r}")
        return self.comps[slug]

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden host")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            page = TEMPLATE.read_text().replace("__KX_TOKEN__", self.token).replace("__KX_DEFAULT__", self.default)
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        parts = u.path.strip("/").split("/")
        if parts[0] != "api" or len(parts) < 2:
            return self._send(404, b"not found", "text/plain")
        try:
            if parts[1] == "comps":
                with LOCK:
                    return self._json(200, [describe(s, p) for s, p in self.comps.items()])
            root = self._root(q)
            with LOCK:
                os.chdir(root)
                if parts[1] == "history":
                    data = history(root, q.get("protocol") or None)
                elif parts[1] == "leaderboard":
                    data = leaderboard(root, q.get("protocol") or None)
                elif parts[1] == "run" and len(parts) == 3:
                    data = run_kx(["show", parts[2], "--curves"]  # a live chart has the curves up to its last sample
                                  + (["--chunk", q["chunk"], "--since", q.get("since", "0")] if q.get("chunk") else []))
                elif parts[1] == "task" and len(parts) == 3:
                    data = run_kx(["task", "show", parts[2]])
                elif parts[1] == "worker" and len(parts) == 3:
                    data = run_kx(["workers", "--show", parts[2]])
                elif parts[1] == "sub" and len(parts) == 3:
                    data = run_kx(["subs", parts[2]])
                elif parts[1] in ROUTES:
                    data = run_kx(ROUTES[parts[1]](q))
                    data = TRIM.get(parts[1], lambda d: d)(data)
                else:
                    return self._send(404, b"not found", "text/plain")
            return self._json(200, data)
        except KxError as e:
            return self._json(400, {"ok": False, "error": str(e), "fix": e.fix})
        except Exception as e:  # noqa: BLE001 - the page shows the error instead of a dead connection
            return self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-KX-Token") != self.token:
            return self._json(403, {"ok": False, "error": "forbidden"})
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("localhost", "127.0.0.1"):
            return self._json(403, {"ok": False, "error": "forbidden origin"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = u.path.strip("/").split("/")
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if parts[:2] != ["api", "sync"]:
            return self._json(404, {"ok": False, "error": "not found"})
        try:
            root = self._root(q)
        except KxError as e:
            return self._json(400, {"ok": False, "error": str(e), "fix": e.fix})
        if not SYNCING.acquire(blocking=False):
            return self._json(409, {"ok": False, "error": "a kx sync is already running"})
        try:  # read-only on Kaggle: submissions, scores, a leaderboard snapshot
            return self._json(*sync(root))
        finally:
            SYNCING.release()


def competitions(dirs=()) -> tuple[dict[str, Path], str]:
    """Every competition this machine has (best effort: kx.ledger.projects), by a name for the picker (its folder's,
    with its parent's when two share one), and the one to show first: the current one, else the most recently used."""
    from kx.ledger import projects
    roots = projects.discover(list(dirs))
    names = {}
    for r in roots:
        names.setdefault(r.name, []).append(r)
    comps = {}
    for r in roots:
        comps[r.name if len(names[r.name]) == 1 else f"{r.parent.name}/{r.name}"] = r
    try:
        here = config.load().main_root.resolve()
    except KxError:
        here = None
    by_root = {v: k for k, v in comps.items()}
    default = by_root.get(here) if here else None
    if default is None:
        default = next((by_root[p] for p in projects.known() if p in by_root), next(iter(comps), ""))
    return comps, default


def serve(port: int = 8765, open_browser: bool = False, dirs=()):
    if os.environ.get("KX_AGENT"):
        raise KxError("the dashboard is for the person driving, not for a worker")
    comps, default = competitions(dirs)
    if not comps:
        raise KxError("no competition found on this machine (a folder with a comp.yaml)",
                      fix="run kx dashboard in a competition, or name where they are: kx dashboard DIR")
    Handler.token, Handler.comps, Handler.default = secrets.token_urlsafe(16), comps, default
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # one thread per connection: a browser's idle
        httpd.daemon_threads = True                                    # one must not hold up the others
    except OSError as e:
        raise KxError(f"cannot listen on 127.0.0.1:{port}: {e}", fix=f"kx dashboard --port {port + 1}") from None
    url = f"http://localhost:{port}/"
    print(f"kx dashboard: {url}  ({len(comps)} competition(s); Ctrl-C stops it)", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def add_command(cmd, Out):
    def cmd_dashboard(a):
        serve(a.port, a.open, a.dirs)
        return None

    s = cmd("dashboard", cmd_dashboard, "local web page: status, runs, tasks, findings, submissions")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open it in the browser")
    s.add_argument("dirs", nargs="*", metavar="DIR", help="folders to look in for competitions too (it finds the ones "
                   "kx has used on this machine and the ones next to them by itself)")
