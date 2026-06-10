"""Loss post-mortem: trace player-0's economy, army, and attackers through a
replay to explain a death without the graphical viewer.

    python tools/post_mortem.py replays/arena/seed_4.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import Counter

ME = "player-0"


def hexdist(a, b):
    # plain axial distance — good enough for "who is near our base" locally
    aq, ar = a
    bq, br = b
    return max(abs(aq - bq), abs(ar - br), abs((-aq - ar) - (-bq - br)))


def main(path: str) -> None:
    turns = [json.loads(line) for line in open(path, encoding="utf-8")]
    prev_my_bases: dict[str, dict] = {}
    for rec in turns:
        snap = rec.get("state_snapshot", {})
        t = snap.get("turn_number", rec.get("turn"))
        ents = snap.get("entities", {})
        if isinstance(ents, list):
            ents = {e["id"]: e for e in ents}
        mine = [e for e in ents.values() if e.get("owner_id") == ME]
        my_units = Counter(e["type"] for e in mine if e["type"] not in
                           ("Base", "Mine", "Barracks", "Factory", "Airbase"))
        my_bld = [e for e in mine if e["type"] in
                  ("Base", "Mine", "Barracks", "Factory", "Airbase")]
        bases = [e for e in my_bld if e["type"] == "Base"]
        gold = snap.get("players", {}).get(ME, {}).get("gold")
        if gold is None:
            gold = snap.get("players", {}).get(ME, {}).get("resources", {}).get("gold")
        alive = snap.get("players", {}).get(ME, {}).get("alive", True)

        # enemies within 4 of any of our bases
        near = Counter()
        for e in ents.values():
            if e.get("owner_id") in (ME, None):
                continue
            if e["type"] in ("Base", "Mine", "Barracks", "Factory", "Airbase"):
                continue
            for b in bases:
                if hexdist((e["q"], e["r"]), (b["q"], b["r"])) <= 4:
                    near[e["owner_id"]] += 1
                    break

        interesting = (
            t is not None
            and (t % 5 == 0 or near or len(bases) != len(prev_my_bases) or not alive)
        )
        if interesting:
            bstr = " ".join(
                f"{b['type']}@({b['q']},{b['r']})hp{b['hp']}"
                + ("" if b.get("is_complete", True) else f"[bld{b.get('construction_turns_remaining','?')}]")
                for b in my_bld
            )
            print(
                f"t{t:>3} gold={gold:>5} units={dict(my_units) or '{}'} "
                f"near_base={dict(near) or '{}'} | {bstr}"
            )
        prev_my_bases = {b["id"]: b for b in bases}
        if not alive:
            print(f"*** ELIMINATED at turn {t}")
            break


if __name__ == "__main__":
    main(sys.argv[1])
