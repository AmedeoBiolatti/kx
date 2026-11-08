# kx for NextCode

A [NextCode](https://github.com/AmedeoBiolatti/nextcode) extension: kx's web dashboard as one native editor tab,
**Dashboard** (palette: **Open kx: Dashboard**), with its pages as tabs, each refreshed on its own:

- **Overview**: what needs you (with the command that fixes it), best local and public score, submission slots and
  what is running as tiles, the score over time (protocol picker; click a point to open its run), any reported metric
  over runs, the leaderboard over time and local against public score, running runs, latest submissions, tasks and
  budgets. A task or submission opens from its row.
- **Runs**: the newest runs, filtered by status, tag, words or "logs curves"; one opened by its row, its id (or a
  point on a chart) with its scores, metrics, config, curves, notes and files, and **Show in Training**, beside the
  list.
- **Training**, a workspace like wandb's. On the left, in place while the charts scroll: the runs that log curves,
  found by words (also a curve's name), status, tag, protocol or task, and ticked to show (up to 6, each with its
  color in the charts), their scores or any curve's last value as bars, and the charts' settings (picked by name,
  added one by one, their size and how many a row). On the right: one x axis for all (step, time since the start, or
  a count the runs log), smoothing and log scale, then the charts side by side, a curve each, in sections by prefix
  (train/, val/) that fold away, each removed by its ×, all sharing their hover line and zoom. Below: the ticked
  runs' scores with `kx compare`'s verdict against the run picked, metrics, last values, and what differs in config
  and code.
- **Tasks**, **Findings**, **Submissions**: the lists, filtered, and a task or submission opened (by its row or id)
  beside the list, with its notes, runs, findings, games and opponents. A task's runs open in Training.

States read at a glance: a run's, a task's and a submission's as icons in its color, `kx compare`'s verdict before a
score, a dot on a config value that differs from the run compared against.

It works with NextCode 0.1.3 and later (extension API 3: `run-process`, process completion callbacks and refresh
timers, as described in [REQUEST.md](REQUEST.md)), and looks as described above in **NextCode 0.1.6**. NextCode
tells the tools it runs which extension API it takes (`NEXT_CODE_EXTENSION_API`, `NEXT_CODE_VERSION`), and kx draws
for it (`HOST` in `src/kx/views/nextcode/widgets.py`):

- **API 4** (NextCode PR #14): tabs, a sidebar beside the charts, grids, folding sections, stat tiles, tables with
  sized columns and clickable or tickable rows (the run list is the charts' legend), series styles, date, duration
  and log axes, controls in a chart's title row (a chart's ×, the protocol picker) and charts that share their hover
  line and zoom.
- **0.1.6** (NextCode PR #15) adds icons in table cells (the states above; elsewhere their words stand in), and lays
  a sidebar layout that is the whole panel out as panes, each scrolling on its own: Training's run list, and the run,
  task or submission opened beside its list, stay in place.
- **API 3** (0.1.3 to 0.1.5): the same pages with workarounds (REQUEST-2 to REQUEST-5 record them, and what they
  asked of NextCode: all of it is in API 4): buttons for tabs, a checkbox a run, charts sized by padding their titles,
  axes converted to plain numbers, the shorter of two columns padded to the other's height.

## Install

1. kx where the project runs (`uv tool install --editable kx`, as in kx's README): on the machine, WSL distribution
   or container NextCode opens the competition repo in.
2. In NextCode, **File > Manage Extensions…**, install this folder (`extension.json` and `kx.wasm`), then **Enable…**
   and approve "run `kx`".
3. Open a competition repo and run **Open kx: Dashboard** from the palette.

## How it works

kx draws the panel and this module only starts it: every invocation of the panel (opening, Refresh, a tab, a filter,
a click, the timer) asks NextCode to run `kx nextcode panel --invocation '<the invocation>'` in the project with
`"response": "stdout"`, so that command's output is the extension's whole response, validated by NextCode without
coming back through WASM. The pages are Python, next to the web dashboard
([src/kx/views/nextcode/](../src/kx/views/nextcode/), one module a page, tested in `tests/test_nextcode.py`), and
`kx.wasm` changes only with the API. It implements NextCode's language-neutral ABI
itself (no SDK dependency), with `serde_json` alone.

The module comes back only when kx fails (not installed, a crash, a timeout), and shows why in the panel; kx itself
turns its errors (not a competition, a broken ledger) into a panel and exits 0. Such a failure reaches the module with
kx's output in the request, and reading it costs the module's fuel: with API 3, past about 32 KiB of stdout or 128 KiB
of stderr it runs out, and NextCode reports an extension error (in Extension output) instead of the panel (API 4 hosts
give 50 times the fuel). kx keeps its own output under 192 KiB with API 3 (NextCode keeps 256 KiB of each stream) and
900 KiB with API 4 (1 MiB), falling back to fewer tasks and chart points, and checks each panel against NextCode's
limits before printing it (`problems` in `widgets.py`).

The manifest stays at API 3, so the package installs in every NextCode since 0.1.3; the newer fields go only to the
hosts that say they take them, and `kx.wasm` didn't change for API 4.

## Build

```bash
rustup target add wasm32-unknown-unknown
cargo test                                                # the module's own tests, native
cargo build --release --target wasm32-unknown-unknown
cp target/wasm32-unknown-unknown/release/kx.wasm kx.wasm  # committed, so installing needs no Rust
```

Then **Reload packages** in the manager. NextCode never overwrites an installed folder, so first copy the new
`extension.json` and `kx.wasm` over the installed ones (`~/.config/next_code/extensions/kx/` on Linux), or remove
that folder and install again.
