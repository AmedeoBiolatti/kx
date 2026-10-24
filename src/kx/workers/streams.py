"""Reading agent harness output: Claude Code `stream-json` and Codex `--json` events, as they stream or from a
transcript. Token counts, turns, the last action and the final message feed `kx workers` and the token budget."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass
class State:
    harness: str
    turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    plan_5h: float | None = None
    last_action: str = "starting"
    text: str = ""
    result: str | None = None
    actions: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def act(self, what: str):
        what = " ".join(str(what).split())[:300]
        self.last_action = what
        self.actions.append((time.time(), what))
        del self.actions[:-200]



def _detail(inp) -> str:
    if not isinstance(inp, dict):
        return str(inp)
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        if inp.get(key):
            return str(inp[key])
    return json.dumps(inp)[:200]


def parse_claude(d: dict, st: State):
    t = d.get("type")
    if t == "assistant":
        msg = d.get("message") or {}
        st.turns += 1
        u = msg.get("usage") or {}
        st.tokens_in += int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) \
            + int(u.get("cache_read_input_tokens") or 0)
        st.tokens_out += int(u.get("output_tokens") or 0)
        for c in msg.get("content") or []:
            if c.get("type") == "tool_use":
                st.act(f"{c.get('name', 'tool')}: {_detail(c.get('input') or {})}")
            elif c.get("type") == "text" and c.get("text", "").strip():
                st.text = c["text"].strip()
                st.act("says: " + st.text[:200])
    elif t == "user":
        for c in (d.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                content = c.get("content")
                st.errors.append((content if isinstance(content, str) else json.dumps(content))[:300])
    elif t == "rate_limit_event":
        w = (d.get("rate_limit_info") or {}).get("unifiedWindows") or {}
        if "five_hour" in w:
            st.plan_5h = w["five_hour"].get("utilization")
    elif t == "result":
        st.result = d.get("result") or st.text
        st.turns = int(d.get("num_turns") or st.turns)
        if d.get("total_cost_usd") is not None:
            st.cost_usd = float(d["total_cost_usd"])


def parse_codex(d: dict, st: State):
    t = d.get("type", "")
    item = d.get("item") or {}
    kind = item.get("type", "")
    if t == "turn.started":
        st.turns += 1
    elif t == "turn.completed":
        u = d.get("usage") or {}
        st.tokens_in += int(u.get("input_tokens") or 0)
        st.tokens_out += int(u.get("output_tokens") or 0) + int(u.get("reasoning_output_tokens") or 0)
    elif t in ("turn.failed", "error"):
        st.errors.append(json.dumps(d.get("error") or d)[:300])
    elif t == "item.started" and kind == "command_execution":
        st.act("shell: " + str(item.get("command", "")))
    elif t == "item.completed":
        if kind == "command_execution" and item.get("exit_code") not in (0, None):
            st.errors.append(f"exit {item.get('exit_code')}: {str(item.get('command'))[:200]}")
        elif kind == "file_change":
            st.act("edit: " + ", ".join(c.get("path", "?") for c in item.get("changes") or []))
        elif kind == "agent_message":
            st.text = (item.get("text") or "").strip()
            st.result = st.text or st.result
            st.act("says: " + st.text[:200])


def parse_line(line: str, st: State):
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        if line.strip():
            st.act("output: " + line.strip())
        return
    if isinstance(d, dict):
        (parse_claude if st.harness == "claude" else parse_codex)(d, st)


def replay(w: dict) -> State:
    st = State(w["harness"])
    try:
        with open(w["transcript"]) as f:
            for line in f:
                parse_line(line, st)
    except (OSError, TypeError):
        pass
    return st

