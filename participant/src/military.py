"""Target selection, movement, defense. Survival doctrine: never start fights,
focus-fire anything that threatens a Base, keep a garrison home, and use Scouts
for eyes (site surveys + exploration) rather than damage.

All movement honours the engine's simultaneous-move rules: we never path into
any tile occupied at turn start (swaps/chains bounce) and we reserve every
planned destination in the shared `claimed` set so two of our own units can't
collide. Attacks fire from the pre-move tile, so attackers simply stand still.
"""

from __future__ import annotations

import time

from engine.actions import AttackAction, MoveAction
from engine.constants import (
    ARTILLERY_SPLASH_RADIUS,
    DIFFICULT_TERRAIN_MOVE_COST,
    ELEVATION_ATTACK_BONUS,
    TREATY_CUTOFF_TURN,
)
from engine.hex_grid import HexCoord

from state import Coord, Ledger, Memory, Snapshot

DEFEND_RADIUS = 5  # threats inside this range of a base trigger a recall
ENGAGE_RADIUS = 4  # we only attack hostiles this close to our buildings (or aggressors)
RETREAT_HP_FRACTION = 0.4


def plan(
    snap: Snapshot, mem: Memory, ledger: Ledger, claimed: set[Coord], deadline: float
) -> list:
    actions: list = []
    grid = snap.grid

    scouts = [u for u in snap.my_units if u["type"] == "Scout"]
    combat = [u for u in snap.my_units if u["type"] not in ("Scout", "Medic")]

    hostiles = [e for e in snap.enemy_units if snap.hostile(e["owner_id"])]
    hostile_all = hostiles + [
        e for e in snap.enemy_buildings if snap.hostile(e["owner_id"])
    ]

    attackers_used = _plan_attacks(snap, mem, actions, combat, hostile_all)
    if time.monotonic() < deadline:
        _plan_combat_moves(
            snap, mem, actions, combat, hostiles, attackers_used, claimed, deadline
        )
    if time.monotonic() < deadline:
        _plan_scout_moves(snap, mem, actions, scouts, claimed, deadline)
    return actions


# ── attacks ───────────────────────────────────────────────────────────────────


def _unit_damage(snap: Snapshot, unit: dict, vs_building: bool = False) -> int:
    power = unit.get("attack_power", 0)
    if snap.terrain.get((unit["q"], unit["r"])) == "elevated":
        power = int(power * ELEVATION_ATTACK_BONUS)
    return power


def _splash_unsafe(snap: Snapshot, target: HexCoord) -> bool:
    """Artillery splash ignores ownership AND treaties — never fire into a ring
    containing our own or a peace partner's entity."""
    ring = snap.grid.ring(target, ARTILLERY_SPLASH_RADIUS)
    ring_set = {(c.q, c.r) for c in ring}
    for e in snap.my_units + snap.my_buildings:
        if (e["q"], e["r"]) in ring_set:
            return True
    for e in snap.enemy_units + snap.enemy_buildings:
        if (e["q"], e["r"]) in ring_set and snap.peace_active(e["owner_id"]):
            return True
    return False


def _plan_attacks(snap, mem, actions, combat, hostile_all) -> set[str]:
    """Greedy focus fire: kill what we can, never poke what we can't (unless it
    is on our doorstep). Returns ids of units that attacked."""
    grid = snap.grid
    my_bld = [HexCoord(b["q"], b["r"]) for b in snap.my_buildings]

    def near_us(e) -> int:
        ec = HexCoord(e["q"], e["r"])
        return min((grid.distance(ec, b) for b in my_bld), default=99)

    # engage only doorstep threats and proven aggressors
    targets = [
        e
        for e in hostile_all
        if near_us(e) <= ENGAGE_RADIUS
        or (e["owner_id"] in mem.aggressors and near_us(e) <= ENGAGE_RADIUS + 2)
        or snap.turn >= TREATY_CUTOFF_TURN
        and near_us(e) <= ENGAGE_RADIUS
    ]
    # closest-to-home, then most killable first
    targets.sort(key=lambda e: (near_us(e), e.get("hp", 999)))

    used: set[str] = set()
    for tgt in targets:
        tc = HexCoord(tgt["q"], tgt["r"])
        cands = []
        for u in combat:
            if u["id"] in used or u.get("attack_range", 0) == 0:
                continue
            d = grid.distance(HexCoord(u["q"], u["r"]), tc)
            if 0 < d <= u.get("attack_range", 0):
                if u["type"] == "Artillery" and _splash_unsafe(snap, tc):
                    continue
                cands.append(u)
        if not cands:
            continue
        cands.sort(key=lambda u: -_unit_damage(snap, u))
        hp = tgt.get("hp", 0)
        dealt = 0
        chosen = []
        for u in cands:
            if dealt >= hp:
                break
            chosen.append(u)
            dealt += _unit_damage(snap, u)
        # chip damage is fine on doorstep threats; otherwise only commit to kills
        if dealt < hp and near_us(tgt) > 2:
            continue
        for u in chosen:
            actions.append(AttackAction(unit_id=u["id"], target=tc))
            used.add(u["id"])
    return used


# ── combat movement ───────────────────────────────────────────────────────────


def _consolidate_turn(snap: Snapshot) -> int:
    if snap.max_turns > TREATY_CUTOFF_TURN:
        return TREATY_CUTOFF_TURN - 10
    return int(0.8 * snap.max_turns)


def _plan_combat_moves(
    snap, mem, actions, combat, hostiles, attackers_used, claimed, deadline
) -> None:
    grid = snap.grid
    bases = snap.my_bases_done + snap.my_bases_building
    if not bases:
        return
    endgame = snap.turn >= _consolidate_turn(snap)

    for u in combat:
        if time.monotonic() > deadline:
            return
        here = HexCoord(u["q"], u["r"])
        hp_frac = u.get("hp", 1) / max(1, u.get("max_hp", 1))

        # wounded units with no kill assignment back off toward home
        if hp_frac < RETREAT_HP_FRACTION and u["id"] not in attackers_used:
            step = _step_away(snap, u, hostiles, claimed)
            if step is not None:
                actions.append(MoveAction(unit_id=u["id"], path=[here, step]))
                claimed.add((step.q, step.r))
            continue
        if u["id"] in attackers_used:
            continue  # attack from where we stand; don't drift out of position

        # home base assignment: nearest base
        base = min(
            bases, key=lambda b: grid.distance(here, HexCoord(b["q"], b["r"]))
        )
        bc = HexCoord(base["q"], base["r"])
        d_home = grid.distance(here, bc)

        # threats near MY assigned base → intercept the closest one
        near_threats = [
            h
            for h in hostiles
            if grid.distance(HexCoord(h["q"], h["r"]), bc) <= DEFEND_RADIUS
        ]
        if near_threats:
            tgt = min(
                near_threats,
                key=lambda h: grid.distance(here, HexCoord(h["q"], h["r"])),
            )
            goal = HexCoord(tgt["q"], tgt["r"])
            if grid.distance(here, goal) > 1:
                _advance(snap, mem, u, goal, claimed, actions)
            continue

        # peacetime / endgame posture: stay within the garrison ring
        ring = 2 if not endgame else 1
        if d_home > ring:
            _advance(snap, mem, u, bc, claimed, actions)
        # else: hold (engine default, no action needed)


# ── scouts ────────────────────────────────────────────────────────────────────


def _plan_scout_moves(snap, mem, actions, scouts, claimed, deadline) -> None:
    if not scouts or mem.freeze_scouts_turn == snap.turn:
        return  # a Base build resolves after movement — keep eyes parked
    grid = snap.grid

    survey_done = False
    for i, u in enumerate(scouts):
        if time.monotonic() > deadline:
            return
        here = HexCoord(u["q"], u["r"])

        # 1) survey mission: walk to (within sight of) the chosen Base site
        if not survey_done and mem.base_site_target is not None:
            survey_done = True
            site = HexCoord(*mem.base_site_target)
            d = grid.distance(here, site)
            if d > 2:
                _advance(snap, mem, u, site, claimed, actions, stop_short=2)
            # within 2: hold — the site is inside our 5-tile vision even with
            # concealment, economy will fire the build when it can
            continue

        # 2) guard a base under construction nearby (it can't defend itself)
        guarding = False
        for b in snap.my_bases_building:
            bc = HexCoord(b["q"], b["r"])
            if grid.distance(here, bc) <= 6:
                if grid.distance(here, bc) > 2:
                    _advance(snap, mem, u, bc, claimed, actions, stop_short=2)
                guarding = True
                break
        if guarding:
            continue

        # 3) frontier exploration
        goal = mem.scout_goals.get(u["id"])
        if goal is None or goal in mem.explored or grid.distance(here, HexCoord(*goal)) <= 1:
            goal = _pick_frontier(snap, mem, here)
            if goal is None:
                continue
            mem.scout_goals[u["id"]] = goal
        _advance(snap, mem, u, HexCoord(*goal), claimed, actions)


def _pick_frontier(snap: Snapshot, mem: Memory, here: HexCoord) -> Coord | None:
    """Nearest unexplored lattice point — coarse 3-step lattice keeps this cheap
    and naturally spreads coverage across the torus."""
    grid = snap.grid
    best, best_d = None, 10**9
    for r in range(0, grid.height, 3):
        for q_off in range(0, grid.width, 3):
            c = grid.wrap(HexCoord(q_off - r // 2, r))
            key = (c.q, c.r)
            if key in mem.explored:
                continue
            d = grid.distance(here, c)
            if 2 <= d < best_d:
                best, best_d = key, d
    return best


# ── movement helpers ──────────────────────────────────────────────────────────


def _move_costs(mem: Memory) -> dict[HexCoord, int]:
    return {
        HexCoord(*c): DIFFICULT_TERRAIN_MOVE_COST
        for c, t in mem.terrain_map.items()
        if t == "difficult"
    }


def _advance(snap, mem, unit, goal, claimed, actions, stop_short: int = 0) -> None:
    """Path toward `goal`, taking as many steps as the unit's movement budget
    allows. Never enters a tile occupied at turn start or already claimed."""
    grid = snap.grid
    here = HexCoord(unit["q"], unit["r"])
    move_range = unit.get("movement_range", 1)
    blocked = {HexCoord(*c) for c in snap.occupied | claimed}
    blocked.discard(here)
    blocked.discard(goal)  # let A* route; we trim the path before the goal tile

    path = grid.shortest_path(here, goal, movement_cost_fn=_move_costs(mem), blocked=blocked)
    steps: list[HexCoord] = [here]
    if path and len(path) > 1:
        budget = move_range
        for nxt in path[1:]:
            cost = (
                DIFFICULT_TERRAIN_MOVE_COST
                if mem.terrain_map.get((nxt.q, nxt.r)) == "difficult"
                else 1
            )
            if cost > budget:
                break
            if (nxt.q, nxt.r) in snap.occupied or (nxt.q, nxt.r) in claimed:
                break  # goal tile itself occupied — stop adjacent
            if stop_short and grid.distance(nxt, goal) < stop_short:
                break
            steps.append(nxt)
            budget -= cost
    else:
        # no path (walled off) — greedy single step that closes distance
        best = None
        best_d = grid.distance(here, goal)
        for n in grid.neighbors(here):
            c = (n.q, n.r)
            if c in snap.occupied or c in claimed:
                continue
            if mem.terrain_map.get(c) == "difficult" and move_range < DIFFICULT_TERRAIN_MOVE_COST:
                continue
            d = grid.distance(n, goal)
            if d < best_d:
                best, best_d = n, d
        if best is not None:
            steps.append(best)

    if len(steps) > 1:
        actions.append(MoveAction(unit_id=unit["id"], path=steps))
        dest = steps[-1]
        claimed.add((dest.q, dest.r))


def _step_away(snap, unit, hostiles, claimed) -> HexCoord | None:
    """One step that maximises distance from the nearest hostile."""
    if not hostiles:
        return None
    grid = snap.grid
    here = HexCoord(unit["q"], unit["r"])
    nearest = min(
        hostiles, key=lambda h: grid.distance(here, HexCoord(h["q"], h["r"]))
    )
    nc = HexCoord(nearest["q"], nearest["r"])
    best, best_d = None, grid.distance(here, nc)
    for n in grid.neighbors(here):
        c = (n.q, n.r)
        if c in snap.occupied or c in claimed:
            continue
        d = grid.distance(n, nc)
        if d > best_d:
            best, best_d = n, d
    return best
