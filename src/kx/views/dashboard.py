"""`kx dashboard`: a local web page over the same data as the CLI's --json output.

Each API call runs the matching kx command in-process (so the page and the terminal never disagree). The page only
reads, except `kx sync` (read-only on Kaggle), which needs the per-process token embedded in the page.
"""

from __future__ import annotations

import io
import json
import os
import secrets
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

ROUTES = {  # path -> kx argv builder from the query string
    "status": lambda q: ["status"],
    "runs": lambda q: ["runs", "-n", q.get("n", "200")] + (["--task", q["task"]] if q.get("task") else [])
    + (["--tag", q["tag"]] if q.get("tag") else []) + (["--curves"] if q.get("curves") else []),
    "compare": lambda q: ["compare", q["a"], q["b"]],
    "board": lambda q: ["board", "-n", q.get("n", "50")] + ([q["protocol"]] if q.get("protocol") else []),
    "protocols": lambda q: ["protocol", "list"],
    "tasks": lambda q: ["task", "list"] + (["--all"] if q.get("all") else []),
    "findings": lambda q: ["finding", "list"] + (["--all"] if q.get("all") else []) + (["--tag", q["tag"]] if q.get("tag") else []),
    "subs": lambda q: ["subs", "-n", q.get("n", "30")],
    "workers": lambda q: ["workers", "--all"],
    "budget": lambda q: ["budget"],
}


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
    a = _parser.parse_args(argv)
    with redirect_stderr(io.StringIO()):
        out = a.func(a)
    return out.data


def history(root: Path, protocol: str | None = None) -> dict:
    """Scores under `protocol` (default: the reference) in run order (discarded runs left out, as on the board), with
    the best so far and each run's reported numeric metrics
    (r.log / logged_metrics.json), for the score-over-time and metrics-over-time charts. `protocols` lists every
    protocol with a scored run, for the picker."""
    from kx.ledger.store import Store, loads, rid
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
        rows = st.all("SELECT r.id, r.created, r.hypothesis, r.metrics, s.value, s.se FROM scores s JOIN runs r ON r.id=s.run_id "
                      f"WHERE s.protocol_id=? AND {live} ORDER BY r.id", (pid,))
        best, pts = None, []
        for r in rows:
            if best is None or (r["value"] > best if direction == "max" else r["value"] < best):
                best = r["value"]
            metrics = loads(r["metrics"]) or {}
            pts.append({"run": rid(r["id"]), "created": r["created"], "value": r["value"], "se": r["se"], "best": best,
                        "hypothesis": r["hypothesis"],
                        "metrics": {k: v for k, v in metrics.items() if number(v)} if isinstance(metrics, dict) else {}})
        return {"reference": ref, "protocol": pid, "protocols": protos, "direction": direction, "points": pts}


def leaderboard(root: Path, protocol: str | None = None) -> dict:
    """The leaderboard side of progress: our public scores per submission, leaderboard snapshots (our rank, top, medal
    lines) taken at each kx sync, the range each medal line can have had before the first (submit.lb_bounds), and local
    vs public score for every run with both, under `protocol` (default: the
    reference), with a least-squares line once there are two."""
    from kx.ledger.store import Store, rid
    from kx.runs.protocol import KAGGLE_PUBLIC, reference
    with Store(config.load(root)) as st:
        ref = reference(st)
        subs = [{"id": r["id"], "created": r["created"], "public": r["public"], "private": r["private"],
                 "run": rid(r["run_id"]) if r["run_id"] else None, "message": r["message"]}
                for r in st.all("SELECT * FROM submissions WHERE public IS NOT NULL ORDER BY created, id")]
        snaps = [dict(r) for r in st.all("SELECT * FROM lb_snapshots ORDER BY ts")]
        bounds = st.setting("lb_bounds")
        protos = [{"id": r["id"], "n": r["n"]} for r in st.all(
            "SELECT s.protocol_id id, COUNT(*) n FROM scores s JOIN scores k ON k.run_id=s.run_id AND k.protocol_id=? "
            "WHERE s.protocol_id NOT LIKE 'kaggle:%' GROUP BY s.protocol_id ORDER BY (s.protocol_id=?) DESC, n DESC",
            (KAGGLE_PUBLIC, ref["id"] or ""))]
        pid = protocol or (ref["id"] if any(p["id"] == ref["id"] for p in protos) else protos[0]["id"] if protos else None)
        pairs = [{"run": rid(r["id"]), "hypothesis": r["hypothesis"], "local": r["local"], "se": r["se"],
                  "public": r["public"], "submissions": r["n"]}
                 for r in st.all("SELECT r.id, r.hypothesis, s.value local, s.se, k.value public, k.n FROM scores s "
                                 "JOIN scores k ON k.run_id=s.run_id AND k.protocol_id=? JOIN runs r ON r.id=s.run_id "
                                 "WHERE s.protocol_id=? ORDER BY r.id", (KAGGLE_PUBLIC, pid))] if pid else []
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
    except Exception:  # noqa: BLE001 - a broken competition still appears in the picker
        return {"slug": name, "path": str(root)}


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
        self._send(code, json.dumps(obj, default=str).encode())

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
                    data = run_kx(["show", parts[2], "--curves"])
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
        try:
            root = self._root(q)
            with LOCK:
                os.chdir(root)
                if parts[:2] == ["api", "sync"]:  # read-only on Kaggle: submissions, scores, a leaderboard snapshot
                    data = run_kx(["sync"])
                else:
                    return self._json(404, {"ok": False, "error": "not found"})
            return self._json(200, data)
        except KxError as e:
            return self._json(400, {"ok": False, "error": str(e), "fix": e.fix})


def serve(port: int = 8765, open_browser: bool = False):
    if os.environ.get("KX_AGENT"):
        raise KxError("the dashboard is for the person driving, not for a worker")
    comp = config.load()
    comps = {}
    for root in config.competitions(config.workspace_of(comp)):
        try:
            config.load(root)
            comps[root.name] = root
        except KxError:
            continue
    comps.setdefault(comp.main_root.name, comp.main_root)
    Handler.token, Handler.comps, Handler.default = secrets.token_urlsafe(16), comps, comp.main_root.name
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
        serve(a.port, a.open)
        return None

    s = cmd("dashboard", cmd_dashboard, "local web page: status, runs, tasks, findings, submissions")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open it in the browser")
