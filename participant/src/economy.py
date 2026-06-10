"""Build order + expansion. The single biggest survival lever is a spare,
hidden Base: we're only eliminated when our last COMPLETED Base dies, so this
planner saves for and places extra Bases on far, quiet tiles before anything
fancy. Everything is budgeted through the shared Ledger; gold for the next Base
is reserved so unit spam can't eat it.

A Base may be built on any empty tile that is VISIBLE this turn, so far sites
need a Scout standing eyes-on — economy picks the site (mem.base_site_target),
military walks a Scout there, and we issue the build the turn the tile is
visible and empty.
"""

from __future__ import annotations

import time

from engine.actions import ConstructBuildingAction, ProduceUnitAction
from engine.constants import BUILDING_STATS, TREATY_CUTOFF_TURN, UNIT_STATS
from engine.hex_grid import HexCoord

from state import Coord, Ledger, Memory, Snapshot

BASE_COST = BUILDING_STATS["Base"].gold_cost
SHORT_GAME_TURNS = 60  # at/below this max_turns, play the compressed build order


def plan(
    snap: Snapshot, mem: Memory, ledger: Ledger, claimed: set[Coord], deadline: float
) -> list:
    actions: list = []
    short = snap.max_turns <= SHORT_GAME_TURNS
    # from here the forced-war endgame is close: hoarded gold is worthless,
    # dump it into redundancy (spare Bases, Barracks, units)
    late = not short and snap.turn >= TREATY_CUTOFF_TURN - 20

    base_intent = len(_intent_sites(snap, mem, "Base"))
    if short:
        target_bases = 2
    elif late:
        # keep founding spares as long as gold allows — every completed Base is
        # another thing 19 hostile players must find and kill
        target_bases = max(3, base_intent + (1 if ledger.gold >= 2 * BASE_COST else 0))
    else:
        target_bases = 2 if snap.turn < 0.25 * snap.max_turns else 3
    # stop founding bases too late to complete (they only count when finished)
    if snap.turn > snap.max_turns - BUILDING_STATS["Base"].build_turns - 3:
        target_bases = 0

    saving_for_base = base_intent < target_bases and len(snap.my_bases_done) >= 1
    if saving_for_base:
        ledger.reserve(BASE_COST)
        _plan_base(snap, mem, ledger, claimed, actions, short)

    _plan_barracks(snap, mem, ledger, claimed, actions, late)
    _plan_scouts(snap, mem, ledger, claimed, actions, short, late, saving_for_base)
    _plan_mines(snap, mem, ledger, claimed, actions, short)
    _plan_infantry(snap, mem, ledger, claimed, actions, short, late)
    if not short and time.monotonic() < deadline:
        _plan_factory(snap, mem, ledger, claimed, actions, late)
    return actions


# ── bases ─────────────────────────────────────────────────────────────────────


def _plan_base(snap, mem, ledger, claimed, actions, short) -> None:
    if not ledger.can(BASE_COST, force=True):
        _ensure_base_site(snap, mem, short)  # keep steering the Scout while saving
        return

    # Founding is OPPORTUNISTIC: any tile visible RIGHT NOW that clears the
    # quality bar gets the Base. Waiting on one designated far site was the
    # bottleneck (gold piled up while the Scout walked / the site churned, and
    # on crowded maps no site ever passed the old hard threat gate). The
    # designated site remains only as the Scout's steering target.
    site = _best_visible_site(snap, mem, claimed, short)
    if site is None:
        target = _ensure_base_site(snap, mem, short)
        if (
            target is not None
            and target in snap.visible
            and target not in snap.occupied
            and target not in claimed
        ):
            site = target
    if site is None:
        return
    ledger.spend(BASE_COST, force=True)
    ledger.release(BASE_COST)
    actions.append(
        ConstructBuildingAction(building_type="Base", coord=HexCoord(*site))
    )
    mem.pending_builds[site] = {"type": "Base", "turn": snap.turn}
    claimed.add(site)
    mem.base_site_target = None
    # the build is validated AFTER movement — keep our scouts (the likely
    # vision source) parked this turn so the tile stays visible
    mem.freeze_scouts_turn = snap.turn


def _required_threat_dist(snap, short) -> int:
    """Minimum distance from known threats for a new Base site. Starts strict,
    relaxes with time: holding out for a perfect site cost us games where NO
    second Base was ever built — a risky Base beats none."""
    if short:
        return 3
    if snap.turn < 25:
        return 8
    if snap.turn < 60:
        return 6
    return 4


def _best_visible_site(snap, mem, claimed, short) -> Coord | None:
    grid = snap.grid
    threats = [HexCoord(*i["coord"]) for i in mem.known_enemy_bases.values()]
    threats += [
        HexCoord(*i["coord"])
        for i in mem.last_seen_enemy.values()
        if snap.turn - i["turn"] <= 15
    ]
    threats = threats[:25]  # cap the distance loop — this runs over all visible tiles
    required = _required_threat_dist(snap, short)
    own = [
        HexCoord(b["q"], b["r"])
        for b in snap.my_bases_done + snap.my_bases_building
    ]
    min_spread = 2 if short else 4  # clustered spares die to the same army

    best, best_score = None, -1e9
    for c in snap.visible:
        if (
            c in snap.occupied
            or c in claimed
            or c in mem.rich_tiles
            or c in mem.pending_builds
        ):
            continue
        hc = HexCoord(*c)
        d_own = min((grid.distance(hc, b) for b in own), default=99)
        if d_own < min_spread:
            continue
        d_threat = min((grid.distance(hc, t) for t in threats), default=30)
        if d_threat < required:
            continue
        score = min(d_threat, 18) * 3.0 + min(d_own, 12) * 0.5
        if mem.terrain_map.get(c) == "difficult":
            score -= 1.0
        if score > best_score:
            best, best_score = c, score
    return best


def _ensure_base_site(snap, mem, short) -> Coord | None:
    # A chosen site is COMMITTED: a far site takes a Scout 10+ turns to walk to,
    # so periodic re-picking guarantees only near (bad) sites ever get built.
    # Re-pick only when the site is invalidated — occupied, a threat moved in,
    # or it's been stale for ages.
    site = mem.base_site_target
    if site is not None:
        # threat-invalidation only EARLY: on a crowded map every site is near
        # someone eventually, and perpetual re-picking meant no Base ever got
        # built. Past turn 30 only an occupied tile invalidates the site.
        threatened = snap.turn < 30 and any(
            snap.grid.distance(HexCoord(*site), HexCoord(*i["coord"])) <= 4
            for i in mem.fresh_threats(snap)
        )
        stale = snap.turn - mem.base_site_turn > 30
        if not (site in snap.occupied or threatened or stale):
            return site
        mem.base_site_target = None

    # don't lock in a site while the map is still tiny — let the Scout push the
    # frontier out first so far candidates exist at all
    if not short and snap.turn < 10 and len(mem.explored) < 120:
        return None

    main = snap.main_base()
    if main is None:
        return None
    main_c = HexCoord(main["q"], main["r"])
    grid = snap.grid

    # stay far from anyone we've ever seen, and a sane scout-walk from home
    threats = [HexCoord(*i["coord"]) for i in mem.known_enemy_bases.values()]
    threats += [
        HexCoord(*i["coord"])
        for i in mem.last_seen_enemy.values()
        if snap.turn - i["turn"] <= 15
    ]
    lo, hi = (2, 7) if short else (7, 17)
    own = {(b["q"], b["r"]) for b in snap.my_buildings}

    best, best_score, best_threat = None, -1e9, 99
    candidates = list(mem.explored)
    step = 2 if len(candidates) > 500 else 1  # sample large maps for speed
    for c in candidates[::step]:
        if c in snap.occupied or c in mem.rich_tiles or c in own or c in claimed_safe(mem):
            continue
        hc = HexCoord(*c)
        d_main = grid.distance(main_c, hc)
        if d_main < lo or d_main > hi:
            continue
        d_threat = min((grid.distance(hc, t) for t in threats), default=20)
        score = min(d_threat, 18) * 3.0 + d_main * 0.5
        if mem.terrain_map.get(c) == "difficult":
            score -= 1.0  # slow to reach / garrison
        if score > best_score:
            best, best_score, best_threat = c, score, d_threat
    if best is not None:
        mem.base_site_target = best
        mem.base_site_turn = snap.turn
        mem.base_site_threat = best_threat
    return mem.base_site_target


def claimed_safe(mem) -> set[Coord]:
    return set(mem.pending_builds)


def _intent_sites(snap, mem, building_type: str) -> set[Coord]:
    """Every tile where a `building_type` of ours stands or is on the way:
    visible buildings (complete or under construction) UNION pending builds.
    The union dedupes — a pending entry now outlives its building's first
    sighting (it is only cleared on COMPLETION), so coords can appear in both."""
    sites = {
        (b["q"], b["r"]) for b in snap.my_buildings if b["type"] == building_type
    }
    sites |= {c for c, i in mem.pending_builds.items() if i["type"] == building_type}
    return sites


# ── support buildings ─────────────────────────────────────────────────────────


def _plan_barracks(snap, mem, ledger, claimed, actions, late=False) -> None:
    count = len(_intent_sites(snap, mem, "Barracks"))
    # late game: more Barracks = more Infantry per turn out of the gold pile
    target = 3 if late and ledger.gold >= 1000 else 1
    if count >= target:
        return
    cost = BUILDING_STATS["Barracks"].gold_cost
    force = count == 0  # the first Barracks enables Scouts — exempt from reserve
    if not ledger.can(cost, force=force):
        return
    site = _adjacent_build_site(snap, mem, claimed)
    if site is None:
        return
    ledger.spend(cost, force=force)
    actions.append(
        ConstructBuildingAction(building_type="Barracks", coord=HexCoord(*site))
    )
    mem.pending_builds[site] = {"type": "Barracks", "turn": snap.turn}
    claimed.add(site)


def _plan_mines(snap, mem, ledger, claimed, actions, short) -> None:
    mines = len(_intent_sites(snap, mem, "Mine"))
    cost = BUILDING_STATS["Mine"].gold_cost

    first = mines == 0

    # a Mine on a rich tile yields 50/turn — worth dipping into the Base reserve
    # for the first one
    if mines < 3:
        for b in snap.my_buildings:
            if not b.get("is_complete", True):
                continue
            for nb in snap.grid.neighbors(HexCoord(b["q"], b["r"])):
                c = (nb.q, nb.r)
                if (
                    c in mem.rich_tiles
                    and c not in snap.occupied
                    and c not in claimed
                    and ledger.can(cost, force=first)
                ):
                    ledger.spend(cost, force=first)
                    actions.append(
                        ConstructBuildingAction(building_type="Mine", coord=nb)
                    )
                    mem.pending_builds[c] = {"type": "Mine", "turn": snap.turn}
                    claimed.add(c)
                    return

    # the FIRST Mine is bought before saving for the Base: staying on 10 gold/turn
    # starves both defense and expansion (a Mine repays itself in 10 turns)
    if first:
        if not ledger.can(cost, force=True):
            return
        site = _adjacent_build_site(snap, mem, claimed)
        if site is None:
            return
        ledger.spend(cost, force=True)
        actions.append(
            ConstructBuildingAction(building_type="Mine", coord=HexCoord(*site))
        )
        mem.pending_builds[site] = {"type": "Mine", "turn": snap.turn}
        claimed.add(site)
        return

    # further plain Mines: long games only, once the expansion is funded
    if short or mines >= 2 or not ledger.can(cost):
        return
    site = _adjacent_build_site(snap, mem, claimed)
    if site is None:
        return
    ledger.spend(cost)
    actions.append(ConstructBuildingAction(building_type="Mine", coord=HexCoord(*site)))
    mem.pending_builds[site] = {"type": "Mine", "turn": snap.turn}
    claimed.add(site)


def _plan_factory(snap, mem, ledger, claimed, actions, late=False) -> None:
    if snap.turn < 40 or len(snap.my_bases_done) < 2:
        return
    have_factory = any(b["type"] == "Factory" for b in snap.my_buildings)
    if not have_factory and not mem.pending_build_count("Factory"):
        cost = BUILDING_STATS["Factory"].gold_cost
        if ledger.can(cost) and ledger.gold - cost >= 200:
            site = _adjacent_build_site(snap, mem, claimed)
            if site is not None:
                ledger.spend(cost)
                actions.append(
                    ConstructBuildingAction(
                        building_type="Factory", coord=HexCoord(*site)
                    )
                )
                mem.pending_builds[site] = {"type": "Factory", "turn": snap.turn}
                claimed.add(site)
        return
    # Artillery (range 3) is what kills bases — and what defends them. Keep a
    # couple in peacetime, more once the forced war is near.
    art = sum(1 for u in snap.my_units if u["type"] == "Artillery")
    art += mem.pending_unit_count("Artillery")
    if art >= (6 if late else 2):
        return
    cost = UNIT_STATS["Artillery"].gold_cost
    for b in snap.my_buildings:
        if b["type"] == "Factory" and b.get("is_complete", True):
            if ledger.can(cost):
                _produce(snap, mem, ledger, claimed, actions, b, "Artillery")
            return


def _adjacent_build_site(snap, mem, claimed) -> Coord | None:
    """Free tile next to a completed own building — never a rich tile (save it
    for a Mine), never boxing the anchor in (keep ≥2 free neighbours so spawns
    and future builds aren't blocked)."""
    for b in snap.my_buildings:
        if not b.get("is_complete", True):
            continue
        # never anchor new construction on a building under siege — it will be
        # razed before it pays for itself
        if mem.threat_near(snap, (b["q"], b["r"]), 4) > 60:
            continue
        bc = HexCoord(b["q"], b["r"])
        free = [
            n
            for n in snap.grid.neighbors(bc)
            if (n.q, n.r) not in snap.occupied and (n.q, n.r) not in claimed
        ]
        for n in free:
            c = (n.q, n.r)
            if c in mem.rich_tiles or c in mem.pending_builds:
                continue
            if len(free) - 1 < 2 and b["type"] in ("Base", "Barracks", "Factory"):
                continue  # would box the producer in
            return c
    return None


# ── units ─────────────────────────────────────────────────────────────────────


def _plan_scouts(snap, mem, ledger, claimed, actions, short, late, saving) -> None:
    alive = sum(1 for u in snap.my_units if u["type"] == "Scout")
    pending = mem.pending_unit_count("Scout")
    # base count is gated by VISION, not gold: a Base needs a visible tile, so
    # more Scouts = more sites surveyed in parallel = more spare Bases
    if short:
        target = 1
    elif late:
        target = 4
    elif saving and snap.turn > 20:
        target = 3
    else:
        target = 2
    if alive + pending >= target:
        return
    cost = UNIT_STATS["Scout"].gold_cost
    force = alive + pending == 0  # the first Scout unlocks the hidden Base
    for b in snap.my_buildings:
        if b["type"] == "Barracks" and b.get("is_complete", True):
            if ledger.can(cost, force=force):
                _produce(snap, mem, ledger, claimed, actions, b, "Scout", force=force)
            return


def _plan_infantry(snap, mem, ledger, claimed, actions, short, late=False) -> None:
    alive = sum(1 for u in snap.my_units if u["type"] == "Infantry")
    pending = mem.pending_unit_count("Infantry")
    n_bases = max(1, len(snap.my_bases_done))
    if short:
        target = 3
    elif late:
        # forced war: surplus gold is dead weight, convert it to bodies.
        # NOT tied to base count — losing a base must never shrink the army cap.
        target = 6 + min(24, ledger.gold // 500)
    elif snap.turn >= 0.6 * snap.max_turns:
        target = min(12, 3 * n_bases + 2)
    else:
        target = min(8, 2 * n_bases + 2)
    per_turn_cap = 6 if late else 2
    cost = UNIT_STATS["Infantry"].gold_cost
    made = 0
    for b in snap.my_buildings:
        if b["type"] != "Barracks" or not b.get("is_complete", True):
            continue
        # don't trickle units into a hopeless siege — bank the gold for the
        # next Base instead (units die one by one against a massed swarm)
        threat = mem.threat_near(snap, (b["q"], b["r"]), 5)
        ours = sum(
            u.get("attack_power", 0)
            for u in snap.my_units
            if u["type"] != "Scout"
            and snap.grid.distance(
                HexCoord(u["q"], u["r"]), HexCoord(b["q"], b["r"])
            )
            <= 5
        )
        if threat > 3 * (ours + UNIT_STATS["Infantry"].attack_power):
            continue
        while alive + pending + made < target and made < per_turn_cap and ledger.can(cost):
            if not _produce(snap, mem, ledger, claimed, actions, b, "Infantry"):
                break
            made += 1
        if made >= per_turn_cap:
            break


def _produce(snap, mem, ledger, claimed, actions, building, unit_type, force=False) -> bool:
    spot = None
    for n in snap.grid.neighbors(HexCoord(building["q"], building["r"])):
        c = (n.q, n.r)
        if c not in snap.occupied and c not in claimed and c not in mem.pending_builds:
            spot = n
            break
    if spot is None:
        return False
    cost = UNIT_STATS[unit_type].gold_cost
    if not ledger.spend(cost, force=force):
        return False
    actions.append(
        ProduceUnitAction(building_id=building["id"], unit_type=unit_type, target=spot)
    )
    claimed.add((spot.q, spot.r))
    mem.pending_production.setdefault(building["id"], []).append(
        {"unit_type": unit_type, "due": snap.turn + UNIT_STATS[unit_type].build_turns}
    )
    return True
