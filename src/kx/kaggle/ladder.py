"""The games a ladder submission played (rl mode): who it played, how each ended, how long it took, and its rating over time.

The games' listing from the Kaggle API carries each game's agents: names, rewards and which one is our submission
(kaggle.episodes), so one call per submission gives every result. Without a known seat our own name is the one present
in most of our games (or comp.yaml `submission.team`).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime

from kx.ledger.store import Store, loads, now


def winrate(wins: float, games: int, draws: float = 0) -> tuple[float, float]:
    """Win rate (draws count half) and its standard error."""
    p = (wins + 0.5 * draws) / games
    return p, math.sqrt(max(p * (1 - p), 1e-12) / games)


def is_ladder_game(row: dict) -> bool:
    """The validation episode Kaggle plays when a submission arrives is not a ladder game."""
    return "VALIDATION" not in str(row.get("type", "")).upper()


def _ts(s: str | None) -> str | None:
    if not s:
        return None
    return s if s.endswith("Z") or "+" in s[10:] else s + "+00:00"


def store_episodes(store: Store, submission_id: str, rows: list[dict], ref: str | None = None) -> int:
    """Insert new games and update state/end of known ones (only what changed reaches the log). When the listing has
    the agents, the players, rewards and our seat (the agent whose submission is `ref`) come with it: no replay
    needed."""
    rows = [r for r in rows if r.get("id") is not None]
    known = {r["episode"] for r in store.all("SELECT episode FROM submission_episodes WHERE submission_id=?",
                                             (submission_id,))}
    with store.transaction():
        for r in rows:
            agents = sorted(r.get("agents") or [], key=lambda a: a.get("index", 0))
            done = str(r.get("state", "")).upper().endswith("COMPLETED") and agents and all(a.get("reward") is not None for a in agents)
            seat = next((i for i, a in enumerate(agents) if ref and str(a.get("submission_id")) == str(ref)), None)
            ep = int(r["id"])
            new = {"ended": _ts(r.get("endTime")), "state": str(r.get("state", "")).split(".")[-1],
                   "seat": seat, "players": json.dumps([str(a.get("team_name")) for a in agents]) if agents else None,
                   "rewards": json.dumps([float(a["reward"]) for a in agents]) if done else None}
            if ep not in known:
                store.insert("submission_episodes", {"submission_id": submission_id, "episode": ep,
                                                     "created": _ts(r.get("createTime")),
                                                     "type": str(r.get("type", "")).split(".")[-1].replace("EPISODE_TYPE_", ""),
                                                     **new}, on="ignore")
            else:  # what a listing leaves out (seat, players, rewards) stays as known
                store.update("submission_episodes", {k: v for k, v in new.items() if v is not None or k in ("ended", "state")},
                             submission_id=submission_id, episode=ep)
    return len(rows)


def our_name(store: Store) -> str | None:
    """Our player name: comp.yaml submission.team, else the name present in most of our games."""
    team = store.comp.submission.get("team")
    if team:
        return str(team)
    seen: Counter = Counter()
    for r in store.x("SELECT players FROM submission_episodes WHERE rewards IS NOT NULL"):
        seen.update(set(loads(r["players"]) or []))
    if not seen:
        return None
    (name, k), *rest = seen.most_common(2) + [(None, 0)]
    return name if rest and k > rest[0][1] else None  # a tie (one game: either seat) is not an answer


def outcome(players: list[str] | None, rewards: list[float] | None, us: str | None) -> str | None:
    """win | loss | tie for `us`, from a game's rewards."""
    if not players or not rewards or us not in players:
        return None
    i = players.index(us)
    others = [x for j, x in enumerate(rewards) if j != i]
    if not others:
        return None
    best = max(others)
    return "win" if rewards[i] > best else "loss" if rewards[i] < best else "tie"


def summary(store: Store, submission_id: str, us: str | None) -> dict | None:
    rows = store.x("SELECT * FROM submission_episodes WHERE submission_id=? AND type != 'VALIDATION' ORDER BY created",
                   (submission_id,)).fetchall()
    if not rows:
        return None
    wlt, opp, secs = Counter(), {}, []
    for r in rows:
        players, rewards = loads(r["players"]), loads(r["rewards"])
        res = outcome(players, rewards, players[r["seat"]] if players and r["seat"] is not None and r["seat"] < len(players) else us)
        if res:
            wlt[res] += 1
            other = next((p for j, p in enumerate(players) if j != (r["seat"] if r["seat"] is not None else players.index(us))), None)
            if other:
                opp.setdefault(other, Counter())[res] += 1
        if r["state"] == "COMPLETED" and r["created"] and r["ended"]:
            try:
                secs.append((datetime.fromisoformat(r["ended"]) - datetime.fromisoformat(r["created"])).total_seconds())
            except ValueError:
                pass
    decided = sum(wlt.values())
    out = {"games": len(rows), "finished": sum(r["state"] == "COMPLETED" for r in rows), "win": wlt["win"],
           "loss": wlt["loss"], "tie": wlt["tie"], "unread": sum(r["state"] == "COMPLETED" and r["rewards"] is None
                                                                  and r["players"] != "[]" for r in rows)}
    if decided:
        p, se = winrate(wlt["win"], decided, wlt["tie"])
        out["winrate"], out["se"] = p, se if math.isfinite(se) else None
    if secs:
        secs.sort()
        out["game_s"] = {"mean": sum(secs) / len(secs), "median": secs[len(secs) // 2], "max": secs[-1]}
    out["opponents"] = sorted(({"name": k, "win": v["win"], "loss": v["loss"], "tie": v["tie"]} for k, v in opp.items()),
                              key=lambda o: -(o["win"] + o["loss"] + o["tie"]))[:8]
    return out


def record_score(store: Store, submission_id: str, public: float | None, episodes: int | None):
    """One row per change of (score, games): the rating's trajectory."""
    last = store.x("SELECT public, episodes FROM submission_scores WHERE submission_id=? ORDER BY ts DESC, rowid DESC LIMIT 1",
                   (submission_id,)).fetchone()
    if public is None or (last and last["public"] == public and last["episodes"] == episodes):
        return
    store.insert("submission_scores", {"submission_id": submission_id, "ts": now(), "public": public, "episodes": episodes})


def history(store: Store, submission_id: str) -> list[dict]:
    return [dict(r) for r in store.x("SELECT ts, public, episodes FROM submission_scores WHERE submission_id=? ORDER BY ts, rowid",
                                     (submission_id,))]
