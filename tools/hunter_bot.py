"""HunterBot — a FIXED sparring opponent for arena testing. NOT submitted.

A deliberately hostile benchmark to measure our agent against a constant
adversary (self-play is a confounded A/B: changing our agent changes all 19
opponents too). It attacks the exact things our strategy depends on:

  - never makes peace (rejects every proposal, proposes nothing)
  - hunts Bases: Scouts sweep the map and every enemy Base ever sighted is
    remembered until seen destroyed
  - masses Infantry from two Barracks and marches waves at the nearest known
    enemy Base; adds siege Artillery (range 3 outranges Infantry garrisons)

Usage:
    python tools/arena.py --games 14 --opponent-agent tools/hunter_bot.py

FREEZE RULE: this file is a benchmark. Never tune it to be easier to beat —
if a stronger sparring partner is needed, create hunter_bot_v2.py instead, so
old numbers stay comparable.

Self-contained on purpose: imports only the engine + agent_base (which resolve
to server/src under the arena), shares NO code with the agent under test.
"""

from __future__ import annotations

import random

from agent_base import PlayerAgent
from engine.actions import (
    ActionPayload,
    AttackAction,
    ConstructBuildingAction,
    MoveAction,
    ProduceUnitAction,
    RespondTreatyAction,
)
from engine.constants import BUILDING_STATS, UNIT_STATS
from engine.hex_grid import HexCoord, HexGrid

WAVE_SIZE = 6  # infantry rallied at home before a wave marches


class HunterBot(PlayerAgent):
    def __init__(self) -> None:
        self.rng: random.Random | None = None
        self.known_bases: dict[str, tuple[int, int]] = {}  # enemy base id -> coord
        self.scout_goals: dict[str, tuple[int, int]] = {}
        self.last_turn = -1

    async def decide(self, observation: dict) -> ActionPayload:
        pid = observation["player_id"]
        turn = observation.get("turn_number", 0)
        if turn < self.last_turn:  # arena reuse — new game, wipe memory
            self.known_bases.clear()
            self.scout_goals.clear()
        self.last_turn = turn
        if self.rng is None:
            # stable per-player seed (hash() is salted per process — don't use it)
            self.rng = random.Random(sum(ord(c) for c in pid) * 9973 + 7)

        gold = observation.get("resources", {}).get("gold", 0)
        grid = HexGrid(
            observation.get("map_width", 35), observation.get("map_height", 30)
        )

        my_units: list[dict] = []
        my_buildings: list[dict] = []
        enemies: list[dict] = []
        occupied: set[tuple[int, int]] = set()
        terrain: dict[tuple[int, int], str] = {}
        present: set[str] = set()
        for tile in observation.get("visible_tiles", []):
            terrain[(tile["q"], tile["r"])] = tile.get("terrain", "normal")
            for e in tile.get("entities", []):
                occupied.add((e["q"], e["r"]))
                present.add(e["id"])
                if e.get("owner_id") == pid:
                    (my_buildings if e["type"] in BUILDING_STATS else my_units).append(e)
                else:
                    enemies.append(e)
                    if e["type"] == "Base":
                        self.known_bases[e["id"]] = (e["q"], e["r"])
        # forget bases we can see are gone
        visible = set(terrain)
        for bid in list(self.known_bases):
            if bid not in present and self.known_bases[bid] in visible:
                del self.known_bases[bid]

        actions: list = []
        claimed: set[tuple[int, int]] = set()
        ledger = [gold]  # single-cell mutable budget

        # ── diplomacy: hard no ────────────────────────────────────────────────
        for p in observation.get("incoming_treaty_proposals", []):
            if isinstance(p, dict) and p.get("proposer_id"):
                actions.append(
                    RespondTreatyAction(
                        proposing_player_id=p["proposer_id"],
                        treaty_type=p.get("treaty_type", "peace"),
                        accept=False,
                    )
                )

        # ── economy: minimal floor, everything else into army ────────────────
        self._build(actions, my_buildings, grid, occupied, claimed, ledger, turn, terrain)
        self._produce(actions, my_units, my_buildings, grid, occupied, claimed, ledger)

        # ── military ──────────────────────────────────────────────────────────
        home = next((b for b in my_buildings if b["type"] == "Base"), None)
        home_c = HexCoord(home["q"], home["r"]) if home else None
        scouts = [u for u in my_units if u["type"] == "Scout"]
        army = [u for u in my_units if u["type"] != "Scout"]

        self._attacks(actions, army + scouts, enemies, grid, terrain, pid)
        self._scout_moves(actions, scouts, grid, terrain, occupied, claimed, visible)
        self._army_moves(actions, army, grid, terrain, occupied, claimed, home_c)

        return ActionPayload(player_id=pid, turn_number=turn, actions=actions)

    # ── economy ───────────────────────────────────────────────────────────────

    def _build(self, actions, my_buildings, grid, occupied, claimed, ledger, turn, terrain):
        complete = [b for b in my_buildings if b.get("is_complete", True)]
        barracks = sum(1 for b in my_buildings if b["type"] == "Barracks")
        mines = sum(1 for b in my_buildings if b["type"] == "Mine")
        factories = sum(1 for b in my_buildings if b["type"] == "Factory")

        want = None
        if barracks < 1 or (barracks < 2 and turn >= 15):
            want = "Barracks"
        elif mines < 1:
            want = "Mine"
        elif factories < 1 and turn >= 40 and ledger[0] >= 500:
            want = "Factory"
        if want is None:
            return
        cost = BUILDING_STATS[want].gold_cost
        if ledger[0] < cost:
            return
        best = None
        for b in complete:
            for n in grid.neighbors(HexCoord(b["q"], b["r"])):
                c = (n.q, n.r)
                if c in occupied or c in claimed:
                    continue
                if want == "Mine" and terrain.get(c) == "rich_resource":
                    best = n  # rich tile: 50/turn instead of 20 — take it
                    break
                if best is None:
                    best = n
            if best is not None and (want != "Mine" or terrain.get((best.q, best.r)) == "rich_resource"):
                break
        if best is None:
            return
        ledger[0] -= cost
        actions.append(ConstructBuildingAction(building_type=want, coord=best))
        claimed.add((best.q, best.r))

    def _produce(self, actions, my_units, my_buildings, grid, occupied, claimed, ledger):
        scouts = sum(1 for u in my_units if u["type"] == "Scout")
        artillery = sum(1 for u in my_units if u["type"] == "Artillery")

        def spawn(b):
            for n in grid.neighbors(HexCoord(b["q"], b["r"])):
                c = (n.q, n.r)
                if c not in occupied and c not in claimed:
                    return n
            return None

        for b in my_buildings:
            if not b.get("is_complete", True):
                continue
            if b["type"] == "Barracks":
                unit = "Scout" if scouts < 2 else "Infantry"
                cost = UNIT_STATS[unit].gold_cost
                if ledger[0] >= cost:
                    spot = spawn(b)
                    if spot is not None:
                        ledger[0] -= cost
                        actions.append(
                            ProduceUnitAction(
                                building_id=b["id"], unit_type=unit, target=spot
                            )
                        )
                        claimed.add((spot.q, spot.r))
                        if unit == "Scout":
                            scouts += 1
            elif b["type"] == "Factory" and artillery < 4:
                cost = UNIT_STATS["Artillery"].gold_cost
                if ledger[0] >= cost:
                    spot = spawn(b)
                    if spot is not None:
                        ledger[0] -= cost
                        actions.append(
                            ProduceUnitAction(
                                building_id=b["id"], unit_type="Artillery", target=spot
                            )
                        )
                        claimed.add((spot.q, spot.r))
                        artillery += 1

    # ── combat ────────────────────────────────────────────────────────────────

    def _attacks(self, actions, units, enemies, grid, terrain, pid):
        if not enemies:
            return
        for u in units:
            ar = u.get("attack_range", 0)
            if ar < 1:
                continue
            here = HexCoord(u["q"], u["r"])
            best, best_key = None, None
            for e in enemies:
                d = grid.distance(here, HexCoord(e["q"], e["r"]))
                if not (0 < d <= ar):
                    continue
                # Bases first, then other buildings, then weakest unit
                rank = (
                    0 if e["type"] == "Base"
                    else 1 if e["type"] in BUILDING_STATS
                    else 2
                )
                key = (rank, e.get("hp", 999))
                if best_key is None or key < best_key:
                    best, best_key = e, key
            if best is None:
                continue
            tc = HexCoord(best["q"], best["r"])
            if u["type"] == "Artillery":
                # don't splash our own melee (engine splash ignores ownership)
                own_adjacent = any(
                    grid.distance(HexCoord(o["q"], o["r"]), tc) <= 1
                    for o in units
                    if o["id"] != u["id"]
                )
                if own_adjacent:
                    continue
            actions.append(AttackAction(unit_id=u["id"], target=tc))

    def _scout_moves(self, actions, scouts, grid, terrain, occupied, claimed, visible):
        for u in scouts:
            here = HexCoord(u["q"], u["r"])
            goal = self.scout_goals.get(u["id"])
            if goal is None or grid.distance(here, HexCoord(*goal)) <= 1:
                goal = (
                    self.rng.randrange(-10, grid.width + 10),
                    self.rng.randrange(0, grid.height),
                )
                w = grid.wrap(HexCoord(*goal))
                goal = (w.q, w.r)
                self.scout_goals[u["id"]] = goal
            self._march(actions, u, HexCoord(*goal), grid, terrain, occupied, claimed)

    def _army_moves(self, actions, army, grid, terrain, occupied, claimed, home_c):
        target = None
        if self.known_bases:
            # nearest known enemy base to the army's center of mass
            ref = (
                HexCoord(army[0]["q"], army[0]["r"]) if army else home_c
            )
            if ref is not None:
                target = HexCoord(
                    *min(
                        self.known_bases.values(),
                        key=lambda c: grid.distance(ref, HexCoord(*c)),
                    )
                )
        marching = target is not None and len(army) >= WAVE_SIZE
        for u in army:
            here = HexCoord(u["q"], u["r"])
            if marching:
                goal = target
            elif home_c is not None and grid.distance(here, home_c) > 3:
                goal = home_c  # rally until the wave is big enough
            else:
                continue
            self._march(actions, u, goal, grid, terrain, occupied, claimed)

    def _march(self, actions, unit, goal, grid, terrain, occupied, claimed):
        """Greedy multi-step toward goal, respecting movement costs and never
        entering a tile occupied at turn start (bounce avoidance)."""
        here = HexCoord(unit["q"], unit["r"])
        budget = unit.get("movement_range", 1)
        steps = [here]
        cur = here
        while budget > 0:
            best, best_d = None, grid.distance(cur, goal)
            best_cost = 1
            for n in grid.neighbors(cur):
                c = (n.q, n.r)
                if c in occupied or c in claimed:
                    continue
                cost = 2 if terrain.get(c) == "difficult" else 1
                if cost > budget:
                    continue
                d = grid.distance(n, goal)
                if d < best_d:
                    best, best_d, best_cost = n, d, cost
            if best is None:
                break
            steps.append(best)
            budget -= best_cost
            cur = best
        if len(steps) > 1:
            actions.append(MoveAction(unit_id=unit["id"], path=steps))
            claimed.add((steps[-1].q, steps[-1].r))
