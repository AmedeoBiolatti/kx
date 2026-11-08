# Request to NextCode: extensions that run a declared program

**Done**: NextCode 0.1.3 has all of it as extension API 3 (its docs/extensions.md, "Project programs and refresh
timers"), and `"response": "stdout"` from the later list too, which the kx extension uses. Kept as the record of why.

For the kx extension (this folder), and anything like it: a panel whose data comes from a command-line tool of the
project. Today an extension sees only the active document and its settings, so it cannot show a project's state. The
kx panel needs three additions to the extension API, none of them specific to kx.

## 1. A `run-process` permission, limited to declared programs

```json
{
  "permissions": ["run-process"],
  "processes": [{"program": "kx"}]
}
```

- `processes` lists the programs the extension may run, by name (no paths, no shell). Approval shows them:
  "Run `kx` in the project". A program not listed is an invalid response, as an undeclared panel is today.
- The host runs the program **in the project the panel belongs to**: on the project's host (local, WSL, dev
  container, SSH) through the agent, in the project root, with the environment its terminals get (so `~/.local/bin`
  is on `PATH`). argv is passed as is: no shell, no globbing. stdin is closed.
- Limits as the host sees fit; suggested: 4 running per extension, `timeout_ms` up to 120 s (default 30 s), stdout
  and stderr up to 4 MiB each, cut beyond that with `truncated: true`. Closing or reloading the panel, disabling the
  extension or cancelling kills the process.

## 2. Responses start processes; their exit is an invocation

Any response to an invocation bound to a panel (`open-panel`, `ui-event`, and the two new kinds below) may carry

```json
{"processes": [{"id": "status", "program": "kx", "args": ["nextcode", "panel"], "timeout_ms": 30000}]}
```

next to (or instead of) `panels`. Each process is bound to that panel. When it ends, the host invokes the extension
with

```json
{
  "kind": "process-exit",
  "panel": "overview",
  "revision": 4,
  "state": {},
  "inputs": {"protocol": "cv5@1a2b3c4d"},
  "process": {
    "id": "status",
    "exit_code": 0,
    "stdout": "...",
    "stderr": "",
    "truncated": false,
    "error": null
  }
}
```

`exit_code` is `null` when the program could not start, timed out or was killed, and `error` says which (e.g.
`"kx: not found on PATH"`). The response may update the originating panel and start more processes, under the same
rules as a `ui-event` response. A panel that was closed or replaced in the meantime drops the result, like a stale
`ui-event` reply.

A response that has neither `panels` nor `processes` leaves the panel as it is, so a refresh can keep what is shown
until new data arrives.

## 3. A refresh timer on a panel

```json
{"panels": [{"id": "overview", "root": {}, "state": {}, "refresh_ms": 10000}]}
```

`refresh_ms` (optional, at least 1000) asks for one `{"kind": "refresh", "panel", "revision", "state", "inputs"}`
invocation that long after this update. It fires once; a panel that wants to keep refreshing sets it again in the next
update. No refresh fires while an invocation or a process of that panel is still running (it waits for them), and
hosts may hold it back while the panel is hidden, firing when the panel shows again.

Whether this is API 3 or an addition to API 2 is NextCode's call. (It became API 3.)

## What kx does with them

The panel's every invocation starts `kx nextcode panel --invocation '<the invocation JSON>'`, and its `process-exit`
returns that command's stdout, which is a complete response: the panel (with `refresh_ms`), logs and any error. The
widgets and limits are API2's as they are.

## Later, not needed now

- `"response": "stdout"` on a process: on exit 0, its stdout is the response, validated as if the module had
  returned it, and the module is not invoked. kx's module does only that, and reading a tool's JSON output inside
  the request's JSON costs it about 70 fuel a byte (it is mostly escapes): kx keeps its output under 48 KiB to stay
  at a third of the 10 million.
- An `open-url` effect, for the Kaggle links the web dashboard has.
- An `open-file` effect (`path`, `line`), to open a run's log or a task's notes from the panel.
- A row event for `table` (`{"kind": "row", "index": 3}`), to open a run or task from its row.
