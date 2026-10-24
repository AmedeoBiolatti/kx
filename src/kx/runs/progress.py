"""Reported run progress and bounded, tolerant reads of live metric files."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISLNK, S_ISREG

from kx.ledger.config import KxError
from kx.ledger.store import loads, parse_ts
from kx.runs.objects import append_jsonl, atomic_json

TAIL_BYTES = 256 * 1024
RESERVED = {'step', 'ts'}


def number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def metrics(values):
    if any(not isinstance(k, str) or not k or k in RESERVED or not number(v) for k, v in values.items()):
        raise KxError('step metrics need nonempty names and finite numeric values; step and ts are reserved')
    return {k: float(v) for k, v in values.items()}


def records(path, max_bytes=TAIL_BYTES):
    """Read the end of a JSONL stream without loading an entire training history."""
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            if size > max_bytes:
                stream.readline()  # discard a record whose beginning is outside the window
            raw = stream.read(max_bytes)
    except OSError:
        return []
    result = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict):
            result.append(row)
    return result


def append_step(directory, step, values):
    if not number(step) or step < 0:
        raise KxError('step must be a finite nonnegative number')
    append_jsonl(Path(directory) / 'metrics.jsonl', {'step': step, 'ts': datetime.now(timezone.utc).isoformat(), **metrics(values)})


def append_summary(directory, values):
    """Keep scalar summary updates (r.log) ordered with step metrics."""
    values = {k: v for k, v in values.items() if k not in RESERVED and number(v)}
    if values:
        append_jsonl(Path(directory) / 'metrics.jsonl', {'ts': datetime.now(timezone.utc).isoformat(), **values})


def write_progress(directory, completed, total=None, *, unit=None, phase=None, values=None, clock=None):
    if not number(completed) or completed < 0:
        raise KxError('completed must be a finite nonnegative number')
    if total is not None and (not number(total) or total <= 0 or completed > total):
        raise KxError('total must be finite and positive, with completed <= total')
    for name, value in (('phase', phase), ('unit', unit)):
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 160):
            raise KxError(f'{name} must be a nonempty string of at most 160 characters')
    values = metrics(values or {})
    path = Path(directory) / 'progress.json'
    try:
        previous = json.loads(path.read_text())
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    for key in ('phase', 'unit'):
        if not isinstance(previous.get(key), str) or not previous[key].strip() or len(previous[key]) > 160:
            previous.pop(key, None)
    phase, unit = phase or previous.get('phase') or 'Running', unit or previous.get('unit') or 'steps'
    same = previous.get('phase') == phase and previous.get('unit') == unit
    if same and total is None:
        total = previous.get('total')
    if total is not None and (not number(total) or total <= 0 or completed > total):
        raise KxError('completed exceeds the current phase total; report a revised total or a new phase')
    timestamp = (clock or datetime.now(timezone.utc)).isoformat()
    reset = not same or not number(previous.get('completed')) or completed < previous['completed']
    row = {'completed': completed, 'total': total, 'phase': phase, 'unit': unit, 'updated': timestamp,
           'started': timestamp if reset else previous.get('started', timestamp),
           'baseline_completed': completed if reset else previous.get('baseline_completed', completed),
           'metrics': {**({} if reset or not isinstance(previous.get('metrics'), dict) else
                         {k: v for k, v in previous['metrics'].items() if k not in RESERVED and number(v)}), **values}}
    atomic_json(path, row)
    if values:
        append_step(directory, completed, values)
    return row


def snapshot(store, row, *, clock=None):
    t = clock or datetime.now(timezone.utc)
    directory = store.run_dir(row['id'])
    summary = loads(row['metrics']) or {}
    latest = {k: v for k, v in summary.items() if number(v)} if isinstance(summary, dict) else {}
    last_step, metric_at = None, None
    for record in records(directory / 'metrics.jsonl'):
        values = {k: v for k, v in record.items() if k not in RESERVED and number(v)}
        if values:
            latest.update(values)
            timestamp = parse_ts(record.get('ts'))
            metric_at = timestamp.isoformat() if timestamp else None
        if number(record.get('step')):
            last_step = record['step']
    progress, warning = None, None
    path = directory / 'progress.json'
    if path.exists():
        try:
            progress = json.loads(path.read_text())
            if not isinstance(progress, dict) or not number(progress.get('completed')) or progress['completed'] < 0:
                raise ValueError('invalid completed count')
            total = progress.get('total')
            if total is not None and (not number(total) or total <= 0 or progress['completed'] > total):
                raise ValueError('invalid total')
            if any(not isinstance(progress.get(k), str) or not progress[k] for k in ('phase', 'unit')):
                raise ValueError('invalid phase or unit')
            # Only transport validated fields; a damaged optional field must not emit NaN JSON.
            raw = progress
            progress = {k: raw.get(k) for k in ('completed', 'total', 'phase', 'unit')}
            for key in ('updated', 'started'):
                timestamp = parse_ts(raw.get(key))
                progress[key] = timestamp.isoformat() if timestamp else None
            progress['baseline_completed'] = raw.get('baseline_completed') if number(raw.get('baseline_completed')) else None
        except (OSError, ValueError, TypeError, KeyError) as error:
            warning, progress = f'Progress unavailable: {error}', None
    progress = dict(progress or {'phase': 'Running', 'unit': 'steps', 'completed': last_step, 'total': None, 'updated': metric_at})
    updated = parse_ts(progress.get('updated'))
    progress['age_s'] = max(0, (t - updated).total_seconds()) if updated else None
    progress['percent'] = 100 * (progress['completed'] / progress['total']) if progress.get('total') else None
    progress['eta_s'] = None
    started = parse_ts(progress.get('started'))
    baseline = progress.get('baseline_completed')
    if row['status'] == 'running' and started and updated and number(baseline) and progress.get('total'):
        elapsed, advanced = (updated - started).total_seconds(), progress['completed'] - baseline
        if elapsed > 0 and advanced > 0 and progress['age_s'] <= 90:
            eta = elapsed * ((progress['total'] - progress['completed']) / advanced)
            progress['eta_s'] = eta if number(eta) else None
    return {'progress': progress, 'latest_metrics': latest, 'metrics_updated': metric_at, 'telemetry_warning': warning}


_MEMO = {}  # kind -> {path: ((mtime_ns, size), value)}, least recently used first


def _memo(kind, path, compute, keep):
    """compute(), or its last result while the file keeps its size and mtime: a finished run's files never change,
    so a long-lived reader (the dashboard) parses each once. `keep`: how many files of this kind to remember."""
    try:
        st = os.stat(path)
    except OSError:
        return compute()
    cache, stamp = _MEMO.setdefault(kind, {}), (st.st_mtime_ns, st.st_size)
    hit = cache.pop(path, None)
    if hit is None or hit[0] != stamp:
        hit = (stamp, compute())
    cache[path] = hit
    while len(cache) > keep:
        del cache[next(iter(cache))]
    return hit[1]


def curves(directory, times=None):
    """{metric: [[step, value], ...]} in the order logged (a step that goes back starts a new pass). With a dict for
    `times`, it gets {metric: [unix seconds or None, ...]} alongside, from each record's ts. The lists are shared
    with later calls: read them, do not change them."""
    path = Path(directory) / 'metrics.jsonl'
    if times is None:
        return _memo('curves', path, lambda: _curves(path, None), keep=4)

    def both():
        t = {}
        return _curves(path, t), t
    result, got = _memo('curves+times', path, both, keep=4)
    times.update(got)
    return result


def _curves(path, times):
    result = {}
    for record in records(path, max_bytes=2 * 1024 * 1024):
        if not number(record.get('step')):
            continue
        t = None
        if times is not None:
            ts = parse_ts(record.get('ts'))
            t = ts.timestamp() if ts else None
        for key, value in record.items():
            if key not in RESERVED and number(value):
                result.setdefault(key, []).append([record['step'], value])
                if times is not None:
                    times.setdefault(key, []).append(t)
    return result


def curve_names(directory, max_bytes=64 * 1024):
    """The metrics a run logs as curves, in the order first seen: from the start and the end of metrics.jsonl only, so
    listing many runs stays cheap (a metric logged only in the middle of a long run is missed)."""
    path = Path(directory) / 'metrics.jsonl'
    return _memo('names', path, lambda: _curve_names(path, max_bytes), keep=4096)


def _curve_names(path, max_bytes):
    try:
        with path.open('rb') as stream:
            head = stream.read(max_bytes)
    except OSError:
        return []
    rows = []
    for line in head.splitlines()[:-1] if len(head) == max_bytes else head.splitlines():  # the last may be cut off
        try:
            rows.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            continue
    if len(head) == max_bytes:
        rows += records(path, max_bytes)
    names = {}
    for record in rows:
        if isinstance(record, dict) and number(record.get('step')):
            for key, value in record.items():
                if key not in RESERVED and number(value):
                    names[key] = None
    return list(names)


def artifacts(directory, limit=100):
    """List run files without following symlinks or exposing tracking internals."""
    root = Path(directory).resolve()
    internal = {'metrics.jsonl', 'progress.json', 'notes.jsonl', 'scores.json', 'logged_metrics.json', 'attach.json',
                'objects.jsonl', 'remote.json', 'run.sh', 'job.json'}  # kx's bookkeeping, not what the run made
    result = []
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (base == str(root) and d in ('env', 'code', 'objects', '.remote'))
                         and d not in ('.git', '.kx-bin', '__pycache__'))
        for name in sorted(files):
            path = Path(base) / name
            if path.parent == root and name in internal or name.startswith(('.progress-', '.kx-write-')):
                continue
            try:
                stat = path.lstat()  # the walk does not enter linked dirs: only a linked file can lead outside
                if S_ISLNK(stat.st_mode):
                    if not path.resolve().is_relative_to(root):
                        continue
                    stat = path.stat()
                if not S_ISREG(stat.st_mode):
                    continue
            except OSError:
                continue
            result.append({'path': str(path.relative_to(root)), 'bytes': stat.st_size})
            if len(result) > limit:
                return result[:limit], True
    return result, False
