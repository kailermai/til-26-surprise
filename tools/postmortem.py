"""Post-mortem for an arena replay: why did we die?

Streams a replay .jsonl and reports, for one player (default player-0):
  - every Base they ever had: where, when founded/completed, when destroyed,
    and WHO destroyed it (attacker player + unit types, from the action logs)
  - their death turn, plus context (gold, units, buildings at the end)
  - how far apart the bases were (was the "hidden" base actually hidden?)

    python tools/postmortem.py replays/arena/seed_6.jsonl
    python tools/postmortem.py replays/arena/seed_6.jsonl --player player-3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_SERVER_SRC = os.path.join(_REPO, "server", "src")
if _SERVER_SRC not in sys.path:
    sys.path.insert(0, _SERVER_SRC)

from engine.hex_grid import HexCoord, HexGrid  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Explain a loss from an arena replay.")
    ap.add_argument("replay", help="path to a replay .jsonl")
    ap.add_argument("--player", default="player-0", help="player to autopsy (default player-0)")
    args = ap.parse_args()
    pid = args.player

    grid: HexGrid | None = None
    prev_entities: dict[str, dict] = {}
    # base_id -> record
    bases: dict[str, dict] = {}
    # base_id -> Counter("attacker/unittype" -> hits)
    killers: dict[str, Counter] = defaultdict(Counter)
    death_turn: int | None = None
    last_alive_snapshot: dict | None = None

    with open(args.replay, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # skip a truncated/partial line (e.g. mid-write) — don't crash
            turn = rec.get("turn", 0)
            snap = rec.get("state_snapshot", {})
            entities = snap.get("entities", {})
            if grid is None:
                grid = HexGrid(rec.get("map_width", 35), rec.get("map_height", 30))

            # attribute this turn's attacks on our bases (positions at turn start)
            our_base_tiles = {
                (e["q"], e["r"]): eid
                for eid, e in prev_entities.items()
                if e.get("owner_id") == pid and e.get("type") == "Base"
            }
            for actor, payload in rec.get("actions", {}).items():
                if actor == pid:
                    continue
                for a in payload.get("actions", []):
                    if a.get("type") != "attack":
                        continue
                    tgt = (a.get("target_q"), a.get("target_r"))
                    if tgt in our_base_tiles:
                        unit = prev_entities.get(a.get("unit_id"), {})
                        utype = unit.get("type", "?")
                        killers[our_base_tiles[tgt]][f"{actor} ({utype})"] += 1

            # track base lifecycle
            for eid, e in entities.items():
                if e.get("owner_id") != pid or e.get("type") != "Base":
                    continue
                b = bases.get(eid)
                if b is None:
                    bases[eid] = b = {
                        "coord": (e["q"], e["r"]),
                        "born": turn,
                        "completed": None,
                        "died": None,
                        "last_hp": e.get("hp"),
                    }
                b["last_hp"] = e.get("hp")
                if b["completed"] is None and e.get("is_complete"):
                    b["completed"] = turn
            for eid, b in bases.items():
                if b["died"] is None and eid not in entities and eid in prev_entities:
                    b["died"] = turn

            alive = snap.get("players", {}).get(pid, {}).get("alive", True)
            if alive:
                last_alive_snapshot = snap
            elif death_turn is None:
                death_turn = turn

            prev_entities = entities

    # ── report ────────────────────────────────────────────────────────────────
    name = os.path.basename(args.replay)
    print(f"post-mortem: {name} | {pid} | "
          + (f"DIED turn {death_turn}" if death_turn else "survived"))
    print("-" * 72)

    coords = [b["coord"] for b in bases.values()]
    print(f" bases over the whole game: {len(bases)}")
    for eid, b in sorted(bases.items(), key=lambda kv: kv[1]["born"]):
        spread = ""
        if grid and len(coords) > 1:
            dmax = max(
                grid.distance(HexCoord(*b["coord"]), HexCoord(*c))
                for c in coords if c != b["coord"]
            )
            spread = f" | farthest other base {dmax} tiles"
        fate = f"DESTROYED turn {b['died']}" if b["died"] else f"stood to the end (hp {b['last_hp']})"
        done = f"completed t{b['completed']}" if b["completed"] is not None else "never completed"
        print(f"  {b['coord']!s:>9}  founded t{b['born']:<3} {done:<16} {fate}{spread}")
        hits = killers.get(eid)
        if hits:
            top = ", ".join(f"{who} x{n}" for who, n in hits.most_common(6))
            print(f"             attackers: {top}")

    if death_turn and last_alive_snapshot:
        ents = [e for e in last_alive_snapshot.get("entities", {}).values()
                if e.get("owner_id") == pid]
        units = Counter(e["type"] for e in ents if e.get("attack_range") is not None
                        or e.get("movement_range") is not None)
        blds = Counter(e["type"] for e in ents if e.get("is_complete") is not None)
        gold = (last_alive_snapshot.get("players", {}).get(pid, {})
                .get("resources", {}).get("gold"))
        print("-" * 72)
        print(f" at the last turn alive: gold={gold} | "
              f"buildings: {dict(blds)} | units: {dict(units)}")
    print("=" * 72)


if __name__ == "__main__":
    main()
