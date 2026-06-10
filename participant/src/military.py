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
    BUILDING_STATS,
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

    # shared per-turn movement context: the terrain-cost dict is built ONCE
    # (rebuilding it per unit was a hidden decide()-time cost), and A* calls are
    # budgeted — units beyond the budget fall back to greedy steps. Scouts go
    # first in priority order, so spend on combat moves after scouts' share.
    ctx = _MoveCtx(costs=_move_costs(mem), astar_left=12)

    attackers_used = _plan_attacks(snap, mem, actions, combat, hostile_all)
    if time.monotonic() < deadline:
        _plan_scout_moves(snap, mem, actions, scouts, claimed, deadline, ctx)
    if time.monotonic() < deadline:
        _plan_combat_moves(
            snap, mem, actions, combat, hostiles, attackers_used, claimed, deadline, ctx
        )
    return actions


class _MoveCtx:
    def __init__(self, costs: dict, astar_left: int) -> None:
        self.costs = costs
        self.astar_left = astar_left


# ── attacks ───────────────────────────────────────────────────────────────────


def _unit_damage(snap: Snapshot, unit: dict, vs_building: bool = False) -> int:
    power = unit.get("attack_power", 0)
    if snap.terrain.get((unit["q"], unit["r"])) == "elevated":
        power = int(power * ELEVATION_ATTACK_BONUS)
    if vs_building and unit.get("type") == "Bomber":
        power = int(power * 4)
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

    def in_weapon_range(e) -> bool:
        ec = HexCoord(e["q"], e["r"])
        return any(
            0 < grid.distance(HexCoord(u["q"], u["r"]), ec) <= u.get("attack_range", 0)
            for u in combat
        )

    def is_objective(e) -> bool:
        return e.get("type") in ("Base", "Barracks", "Factory", "Airbase")

    # Engage doorstep threats, and let expeditionary units attack strategic
    # buildings once they reach them. Otherwise an army that walks to an enemy
    # Base would hold fire because the target is not near one of our buildings.
    targets = [
        e
        for e in hostile_all
        if near_us(e) <= ENGAGE_RADIUS
        or (e["owner_id"] in mem.aggressors and near_us(e) <= ENGAGE_RADIUS + 2)
        # at war = shoot on sight: strike forces walking to an aggressor's base
        # must fight the defenders shooting them, not march past politely
        or (e["owner_id"] in mem.aggressors and in_weapon_range(e))
        or snap.turn >= TREATY_CUTOFF_TURN
        and near_us(e) <= ENGAGE_RADIUS
        or (
            is_objective(e)
            and in_weapon_range(e)
            and (snap.turn >= TREATY_CUTOFF_TURN - 40 or e["id"] in mem.known_enemy_bases)
        )
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
        vs_building = tgt.get("type") in BUILDING_STATS
        cands.sort(key=lambda u: -_unit_damage(snap, u, vs_building))
        hp = tgt.get("hp", 0)
        dealt = 0
        chosen = []
        for u in cands:
            if dealt >= hp:
                break
            chosen.append(u)
            dealt += _unit_damage(snap, u, vs_building)
        # chip damage is fine on doorstep threats; otherwise only commit to kills
        if dealt < hp and near_us(tgt) > 2 and not is_objective(tgt):
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
    snap, mem, actions, combat, hostiles, attackers_used, claimed, deadline, ctx
) -> None:
    grid = snap.grid
    bases = snap.my_bases_done + snap.my_bases_building
    if not bases:
        return
    endgame = snap.turn >= _consolidate_turn(snap)

    # KEEPS doctrine: garrison only the 2 most defensible bases — the ones
    # farthest from known enemy bases. Smearing a small army across many spares
    # guards nothing (seed 2: 15 bases, zero garrison anywhere); the un-kept
    # spares are decoys that cost enemies time, not forts to die for.
    if len(bases) > 2:
        enemy_bases = [HexCoord(*i["coord"]) for i in mem.known_enemy_bases.values()]

        def keep_score(b):
            bc = HexCoord(b["q"], b["r"])
            return min((grid.distance(bc, e) for e in enemy_bases), default=20)

        bases = sorted(bases, key=keep_score, reverse=True)[:2]

    # per-base balance of power: hostile attack power vs ours, both within
    # DEFEND_RADIUS. A base where we're outgunned 3:1 is lost — evacuate
    # defenders to the safest base instead of feeding them in one at a time.
    def power_near(bc: HexCoord, units: list[dict]) -> int:
        return sum(
            e.get("attack_power", 0)
            for e in units
            if grid.distance(HexCoord(e["q"], e["r"]), bc) <= DEFEND_RADIUS
        )

    base_threat: dict[int, int] = {}
    base_overwhelmed: dict[int, bool] = {}
    for i, b in enumerate(bases):
        bc = HexCoord(b["q"], b["r"])
        threat = power_near(bc, hostiles)
        ours = power_near(bc, combat)
        base_threat[i] = threat
        base_overwhelmed[i] = threat > 3 * (ours + 30)
    safest = min(range(len(bases)), key=lambda i: base_threat[i])
    offensive_goals = [
        info for info in mem.known_enemy_bases.values() if snap.hostile(info["owner"])
    ]
    departures: dict[int, int] = {i: 0 for i in range(len(bases))}

    for u in combat:
        if time.monotonic() > deadline:
            return
        here = HexCoord(u["q"], u["r"])
        hp_frac = u.get("hp", 1) / max(1, u.get("max_hp", 1))

        # wounded units with no kill assignment back off toward home — UNLESS
        # they're standing on a base's wall: a dying body on the ring still
        # blocks a firing slot for turns, stepping away opens it immediately
        on_wall = any(
            grid.distance(here, HexCoord(b["q"], b["r"])) <= 1 for b in bases
        )
        if hp_frac < RETREAT_HP_FRACTION and u["id"] not in attackers_used and not on_wall:
            step = _step_away(snap, u, hostiles, claimed)
            if step is not None:
                actions.append(MoveAction(unit_id=u["id"], path=[here, step]))
                claimed.add((step.q, step.r))
            continue
        if u["id"] in attackers_used:
            continue  # attack from where we stand; don't drift out of position

        # home base assignment: nearest base
        bi = min(
            range(len(bases)),
            key=lambda i: grid.distance(here, HexCoord(bases[i]["q"], bases[i]["r"])),
        )
        base = bases[bi]
        bc = HexCoord(base["q"], base["r"])
        d_home = grid.distance(here, bc)

        # a lost cause is not defended — fall back to the safest base
        if base_overwhelmed[bi] and safest != bi:
            sc = HexCoord(bases[safest]["q"], bases[safest]["r"])
            if grid.distance(here, sc) > 2:
                _advance(snap, mem, u, sc, claimed, actions, ctx)
            continue

        # threats near MY assigned base → RING defense: occupy the 6 tiles
        # adjacent to the Base and HOLD. Every base-killer here is range 1, so
        # a full ring means the Base cannot be hit at all — attackers must chew
        # through 100 HP bodies that shoot back. Never chase: pursuing Scouts
        # (move 3) with Infantry (move 1) drags the garrison out of position
        # while the Base takes chip damage (seed 12: died with 6 idle Infantry).
        near_threats = [
            h
            for h in hostiles
            if grid.distance(HexCoord(h["q"], h["r"]), bc) <= DEFEND_RADIUS
        ]
        if near_threats:
            if d_home <= 1:
                continue  # on the wall — hold it, the attack pass shoots from here
            ring = [
                n
                for n in grid.neighbors(bc)
                if (n.q, n.r) not in snap.occupied and (n.q, n.r) not in claimed
            ]
            if ring:
                spot = min(ring, key=lambda n: grid.distance(here, n))
                _advance(snap, mem, u, spot, claimed, actions, ctx)
                continue
            # no free slot (our own buildings usually fill the ring) — but an
            # enemy STANDING next to our Base is a committed besieger, not a
            # kiter: step to contact and kill it. Parking at distance 2 left
            # range-1 defenders watching Scouts chip the Base down (seed 12).
            besiegers = [
                h
                for h in near_threats
                if grid.distance(HexCoord(h["q"], h["r"]), bc) <= 1
            ]
            if besiegers:
                tgt = min(
                    besiegers,
                    key=lambda h: grid.distance(here, HexCoord(h["q"], h["r"])),
                )
                # goal tile is occupied by the enemy — _advance stops adjacent,
                # which is exactly firing range
                _advance(
                    snap, mem, u, HexCoord(tgt["q"], tgt["r"]), claimed, actions, ctx
                )
            elif d_home > 2:
                _advance(snap, mem, u, bc, claimed, actions, ctx)  # second layer
            continue

        # Once the war window approaches, don't let every unit idle in a tight
        # garrison. Keep a floor at home and send surplus combat power toward
        # remembered enemy Bases to reduce the pressure coming at us.
        local_combat = sum(
            1
            for v in combat
            if grid.distance(HexCoord(v["q"], v["r"]), bc) <= DEFEND_RADIUS
        )
        desired_garrison = 4 if endgame else 3
        offensive_type = u["type"] in ("Tank", "Artillery", "Fighter", "Bomber")
        if offensive_goals and local_combat - departures[bi] > desired_garrison:
            # punish whoever is hitting us first, nearest first: a dead
            # neighbour stops sending waves permanently (a one-base rusher is
            # ELIMINATED outright and its army decays) — the moat strategy
            tgt = min(
                offensive_goals,
                key=lambda i: (
                    i["owner"] not in mem.aggressors,
                    grid.distance(here, HexCoord(*i["coord"])),
                ),
            )
            goal = HexCoord(*tgt["coord"])
            counter_raid = (
                tgt["owner"] in mem.aggressors
                and len(combat) > 8
                and grid.distance(here, goal) <= 12
            )
            campaign = (
                endgame or snap.turn >= TREATY_CUTOFF_TURN - 40 or len(combat) > 12
            ) and (offensive_type or len(combat) > 16)
            if counter_raid or campaign:
                if grid.distance(here, goal) > max(1, u.get("attack_range", 1)):
                    _advance(snap, mem, u, goal, claimed, actions, ctx)
                departures[bi] += 1
                continue

        # peacetime / endgame posture: stay within the garrison ring
        ring = 2 if not endgame else 1
        if d_home > ring:
            _advance(snap, mem, u, bc, claimed, actions, ctx)
        # else: hold (engine default, no action needed)


# ── scouts ────────────────────────────────────────────────────────────────────


def _plan_scout_moves(snap, mem, actions, scouts, claimed, deadline, ctx) -> None:
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
                _advance(snap, mem, u, site, claimed, actions, ctx, stop_short=2)
            # within 2: hold — the site is inside our 5-tile vision even with
            # concealment, economy will fire the build when it can
            continue

        # 2) guard a base under construction nearby (it can't defend itself)
        guarding = False
        for b in snap.my_bases_building:
            bc = HexCoord(b["q"], b["r"])
            if grid.distance(here, bc) <= 6:
                if grid.distance(here, bc) > 2:
                    _advance(snap, mem, u, bc, claimed, actions, ctx, stop_short=2)
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
        _advance(snap, mem, u, HexCoord(*goal), claimed, actions, ctx)


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


def _advance(snap, mem, unit, goal, claimed, actions, ctx, stop_short: int = 0) -> None:
    """Path toward `goal`, taking as many steps as the unit's movement budget
    allows. Never enters a tile occupied at turn start or already claimed.
    A* is rationed via ctx (it's the decide()-time hot spot with a big army);
    over-budget units take greedy steps instead."""
    grid = snap.grid
    here = HexCoord(unit["q"], unit["r"])
    move_range = unit.get("movement_range", 1)

    path = None
    if ctx.astar_left > 0:
        ctx.astar_left -= 1
        blocked = {HexCoord(*c) for c in snap.occupied | claimed}
        blocked.discard(here)
        blocked.discard(goal)  # let A* route; we trim the path before the goal tile
        path = grid.shortest_path(
            here, goal, movement_cost_fn=ctx.costs, blocked=blocked
        )
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
    move_range = unit.get("movement_range", 1)
    best, best_d = None, grid.distance(here, nc)
    for n in grid.neighbors(here):
        c = (n.q, n.r)
        if c in snap.occupied or c in claimed:
            continue
        # an unaffordable difficult step makes the engine drop the WHOLE move —
        # the "retreating" unit would just stand still in attack range
        if (
            snap.terrain.get(c) == "difficult"
            and move_range < DIFFICULT_TERRAIN_MOVE_COST
        ):
            continue
        d = grid.distance(n, nc)
        if d > best_d:
            best, best_d = n, d
    return best
