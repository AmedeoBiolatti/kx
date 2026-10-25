"""Run files written safely (atomic JSON, JSONL appends) and tables logged by runs (r.log_table), stored under
objects/ by content."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from kx.ledger.config import KxError
from kx.ledger.store import now

MAX_OBJECT = 20 * 1024 * 1024


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, allow_nan=False, sort_keys=True, ensure_ascii=False).encode()
    fd, temporary = tempfile.mkstemp(prefix='.kx-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def append_jsonl(path, row: dict):
    try:
        line = json.dumps(row, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise KxError(f'values must be finite JSON: {error}') from None
    with Path(path).open('a') as stream:
        stream.write(line + '\n')


def _cell(value):
    if hasattr(value, 'item'):
        value = value.item()
    if value is None or isinstance(value, (str, bool, int)) or (isinstance(value, float) and math.isfinite(value)):
        return value
    raise KxError('table cells must be finite numbers, strings, booleans or null')


def log_table(run, name, rows, columns=None) -> dict:
    """A table (a list of dicts, of rows with `columns`, or a DataFrame) shown with the run in kx show and the dashboard."""
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise KxError('table name needs 1..200 characters')
    if hasattr(rows, 'to_dict'):
        rows = rows.to_dict(orient='records')
    if not isinstance(rows, (list, tuple)) or len(rows) > 10000:
        raise KxError('table needs a list of at most 10000 rows')
    if columns is None:
        if not rows or not isinstance(rows[0], dict):
            raise KxError('supply columns for an empty table or sequence rows')
        columns = list(rows[0])
    if not isinstance(columns, (list, tuple)) or not 1 <= len(columns) <= 100 or len(set(columns)) != len(columns) \
            or any(not isinstance(c, str) or not c for c in columns):
        raise KxError('table needs 1..100 unique column names')
    cells = []
    for row in rows:
        if isinstance(row, dict):
            if set(row) != set(columns):
                raise KxError('every table row must have the declared columns')
            row = [row[c] for c in columns]
        if not isinstance(row, (list, tuple)) or len(row) != len(columns):
            raise KxError('table row width differs from columns')
        cells.append([_cell(x) for x in row])
    value = {'columns': list(columns), 'rows': cells}
    data = json.dumps(value, allow_nan=False, sort_keys=True, ensure_ascii=False).encode()
    if len(data) > MAX_OBJECT:
        raise KxError('table exceeds the 20 MiB limit')
    sha = hashlib.sha256(data).hexdigest()
    path = Path(run.dir) / 'objects' / f'{sha}.json'
    if not path.exists():
        atomic_json(path, value)
    record = {'id': hashlib.sha256(f'table:{name}:{sha}'.encode()).hexdigest()[:24], 'name': name, 'kind': 'table',
              'path': f'objects/{sha}.json', 'sha256': sha, 'bytes': path.stat().st_size, 'created': now(),
              'metadata': {'rows': len(cells)}}
    append_jsonl(Path(run.dir) / 'objects.jsonl', record)
    return record


def objects(directory, *, preview=False) -> list[dict]:
    """The run's logged tables (the latest 100), each with a preview (first 100 rows, 20 columns) when asked."""
    from kx.runs.progress import records
    root, result = Path(directory).resolve(), {}
    for row in records(root / 'objects.jsonl', max_bytes=2 * 1024 * 1024):
        if row.get('kind') != 'table' or not all(isinstance(row.get(k), str) for k in ('id', 'name', 'path', 'sha256')):
            continue
        item = {k: row.get(k) for k in ('id', 'name', 'kind', 'path', 'sha256', 'bytes', 'created', 'metadata')}
        path = (root / row['path']).resolve()
        item['valid'] = path.is_relative_to(root) and path.is_file() and path.stat().st_size == row.get('bytes')
        if preview and item['valid']:
            try:
                data = path.read_bytes()
                if len(data) > MAX_OBJECT or hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise ValueError('changed')
                value = json.loads(data)
                item['preview'] = {'columns': value['columns'][:20], 'rows': [r[:20] for r in value['rows'][:100]],
                                   'truncated': len(value['rows']) > 100 or len(value['columns']) > 20}
            except (OSError, ValueError, KeyError, TypeError):
                item['valid'] = False
        result[row['id']] = item
    return list(result.values())[-100:]
