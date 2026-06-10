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
WAR_PREP_TURN = TREATY_CUTOFF_TURN - 60
OVERFLOW_GOLD = 1000
BIG_OVERFLOW_GOLD = 3000
# Early-defense floor: a turn-30 rush kills a Base defended by 1–2 Infantry
# (confirmed vs hunter_bot — see CLAUDE.md). Stand up a minimum garrison BEFORE
# saving gold for hidden Bases; naked expansion just hands a rusher a free kill.
EARLY_DEFENSE_FLOOR = 5  # Infantry to have standing/pending before saving for a Base
EARLY_DEFENSE_BY_TURN = 80  # enforce the floor through the early game


def plan(
    snap: Snapshot, mem: Memory, ledger: Ledger, claimed: set[Coord], deadline: float
) -> list:
    actions: list = []
    short = snap.max_turns <= SHORT_GAME_TURNS
    war_prep = not short and snap.turn >= WAR_PREP_TURN
    late = not short and snap.turn >= TREATY_CUTOFF_TURN - 20
    overflow = ledger.gold >= OVERFLOW_GOLD

    # Infrastructure comes before spare-Base reservation. The loss mode we saw
    # was 29k gold with no production buildings, so rebuild capacity first.
    _plan_barracks(snap, mem, ledger, claimed, actions, short, war_prep, late, overflow)
    _plan_scouts(snap, mem, ledger, claimed, actions, short, late, False)
    _plan_mines(snap, mem, ledger, claimed, actions, short)
    if not short and time.monotonic() < deadline:
        _plan_factory(snap, mem, ledger, claimed, actions, war_prep, late, overflow)
    if not short and time.monotonic() < deadline:
        _plan_airbase(snap, mem, ledger, claimed, actions, war_prep, late, overflow)

    # EARLY DEFENSE FLOOR: until a minimum garrison stands, defense outranks
    # expansion. A hidden Base founded while home has 1 Infantry is worthless —
    # the rush kills the main Base before the spare matters.
    defenders = (
        sum(1 for u in snap.my_units if u["type"] == "Infantry")
        + mem.pending_unit_count("Infantry")
    )
    need_defense = (
        not short
        and snap.turn <= EARLY_DEFENSE_BY_TURN
        and defenders < EARLY_DEFENSE_FLOOR
    )

    production_ready = _production_ready(snap, mem, war_prep, late)
    base_intent = len(_intent_sites(snap, mem, "Base"))
    target_bases = _target_base_count(
        snap, short, war_prep, late, overflow, production_ready, ledger.gold
    )
    # stop founding bases too late to complete (they only count when finished)
    if snap.turn > snap.max_turns - BUILDING_STATS["Base"].build_turns - 3:
        target_bases = 0

    # don't reserve gold for a hidden Base while the home garrison is below the
    # floor — that reservation is exactly what starved early Infantry (turn-30 death)
    saving_for_base = (
        base_intent < target_bases
        and len(snap.my_bases_done) >= 1
        and not need_defense
    )
    if saving_for_base:
        ledger.reserve(BASE_COST)
        _plan_base(snap, mem, ledger, claimed, actions, short)

    _plan_scouts(snap, mem, ledger, claimed, actions, short, late, saving_for_base)
    _plan_infantry(
        snap, mem, ledger, claimed, actions, short, war_prep, late, overflow, need_defense
    )
    if not short and time.monotonic() < deadline:
        _plan_heavy_units(snap, mem, ledger, claimed, actions, war_prep, late, overflow)
    if not short and time.monotonic() < deadline:
        _plan_air_units(snap, mem, ledger, claimed, actions, war_prep, late, overflow)
    return actions


def _target_base_count(
    snap: Snapshot,
    short: bool,
    war_prep: bool,
    late: bool,
    overflow: bool,
    production_ready: bool,
    gold: int,
) -> int:
    """Spread extra Bases through peacetime; avoid naked post-200 Base spam.

    Hidden Bases must already be standing before forced war. After the cutoff,
    extra Bases are only worth chasing once production is healthy enough to turn
    surplus gold into defenders.
    """
    if short:
        return 2
    if snap.turn < 50:
        return 2
    if snap.turn < 100:
        return 4
    if snap.turn < WAR_PREP_TURN:
        return 5
    if not late:
        return 7
    target = 6
    if production_ready and overflow:
        target += min(4, gold // BIG_OVERFLOW_GOLD)
    return target


def _production_ready(snap: Snapshot, mem: Memory, war_prep: bool, late: bool) -> bool:
    barracks = len(_intent_sites(snap, mem, "Barracks"))
    factories = len(_intent_sites(snap, mem, "Factory"))
    if late:
        return barracks >= 4 and factories >= 2
    if war_prep:
        return barracks >= 3 and factories >= 1
    return barracks >= 1


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


def _plan_barracks(
    snap, mem, ledger, claimed, actions, short, war_prep=False, late=False, overflow=False
) -> None:
    count = len(_intent_sites(snap, mem, "Barracks"))
    bases = max(1, len(_intent_sites(snap, mem, "Base")))
    if short:
        target = 1
    elif late or overflow:
        target = min(8, max(4, bases + 2))
    elif war_prep:
        target = min(5, max(3, bases))
    elif snap.turn >= 12:
        target = 2
    else:
        target = 1
    cost = BUILDING_STATS["Barracks"].gold_cost
    per_turn = 4 if late or overflow else (2 if war_prep else 1)
    built = 0
    while count < target and built < per_turn:
        opening_second_barracks = snap.turn >= 12 and count < 2
        spend_force = count == 0 or opening_second_barracks or overflow or late
        high_pressure = overflow or late
        if not ledger.can(cost, force=spend_force):
            return
        site = _adjacent_build_site(
            snap, mem, claimed, allow_threat=high_pressure, keep_spawns=not high_pressure
        )
        if site is None:
            return
        ledger.spend(cost, force=spend_force)
        actions.append(
            ConstructBuildingAction(building_type="Barracks", coord=HexCoord(*site))
        )
        mem.pending_builds[site] = {"type": "Barracks", "turn": snap.turn}
        claimed.add(site)
        count += 1
        built += 1


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


def _plan_factory(
    snap, mem, ledger, claimed, actions, war_prep=False, late=False, overflow=False
) -> None:
    if snap.turn < 40 and not overflow:
        return
    count = len(_intent_sites(snap, mem, "Factory"))
    if late or overflow:
        target = 3
    elif war_prep:
        target = 2
    else:
        target = 1
    cost = BUILDING_STATS["Factory"].gold_cost
    per_turn = 2 if late or overflow or war_prep else 1
    built = 0
    while count < target and built < per_turn:
        opening_factory = snap.turn >= 40 and count == 0
        spend_force = opening_factory or overflow or late
        high_pressure = overflow or late
        if not ledger.can(cost, force=spend_force):
            return
        site = _adjacent_build_site(
            snap, mem, claimed, allow_threat=high_pressure, keep_spawns=not high_pressure
        )
        if site is None:
            return
        ledger.spend(cost, force=spend_force)
        actions.append(
            ConstructBuildingAction(building_type="Factory", coord=HexCoord(*site))
        )
        mem.pending_builds[site] = {"type": "Factory", "turn": snap.turn}
        claimed.add(site)
        count += 1
        built += 1


def _plan_airbase(
    snap, mem, ledger, claimed, actions, war_prep=False, late=False, overflow=False
) -> None:
    if snap.turn < 120 and ledger.gold < BIG_OVERFLOW_GOLD:
        return
    count = len(_intent_sites(snap, mem, "Airbase"))
    if late and ledger.gold >= BIG_OVERFLOW_GOLD:
        target = 2
    elif war_prep and ledger.gold >= 2000:
        target = 1
    elif overflow and ledger.gold >= BIG_OVERFLOW_GOLD:
        target = 1
    else:
        return
    cost = BUILDING_STATS["Airbase"].gold_cost
    built = 0
    while count < target and built < 1:
        force = overflow or late
        if not ledger.can(cost, force=force):
            return
        site = _adjacent_build_site(
            snap, mem, claimed, allow_threat=force, keep_spawns=not force
        )
        if site is None:
            return
        ledger.spend(cost, force=force)
        actions.append(
            ConstructBuildingAction(building_type="Airbase", coord=HexCoord(*site))
        )
        mem.pending_builds[site] = {"type": "Airbase", "turn": snap.turn}
        claimed.add(site)
        count += 1
        built += 1


def _adjacent_build_site(
    snap, mem, claimed, allow_threat=False, keep_spawns=True
) -> Coord | None:
    """Free tile next to a completed own building — never a rich tile (save it
    for a Mine), never boxing the anchor in (keep ≥2 free neighbours so spawns
    and future builds aren't blocked)."""
    for b in snap.my_buildings:
        if not b.get("is_complete", True):
            continue
        # never anchor new construction on a building under siege — it will be
        # razed before it pays for itself
        if not allow_threat and mem.threat_near(snap, (b["q"], b["r"]), 4) > 60:
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
            if (
                keep_spawns
                and len(free) - 1 < 2
                and b["type"] in ("Base", "Barracks", "Factory", "Airbase")
            ):
                continue  # would box the producer in
            return c
    return None


# ── units ─────────────────────────────────────────────────────────────────────


def _plan_scouts(snap, mem, ledger, claimed, actions, short, late, saving) -> None:
    alive = sum(1 for u in snap.my_units if u["type"] == "Scout")
    pending = mem.pending_unit_count("Scout")
    infantry = sum(1 for u in snap.my_units if u["type"] == "Infantry")
    infantry += mem.pending_unit_count("Infantry")
    # base count is gated by VISION, not gold: a Base needs a visible tile, so
    # more Scouts = more sites surveyed in parallel = more spare Bases
    if short:
        target = 1
    elif snap.turn < 35 and infantry < 5:
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


def _plan_infantry(
    snap, mem, ledger, claimed, actions, short, war_prep=False, late=False,
    overflow=False, need_defense=False,
) -> None:
    alive = sum(1 for u in snap.my_units if u["type"] == "Infantry")
    pending = mem.pending_unit_count("Infantry")
    n_bases = max(1, len(snap.my_bases_done))
    if short:
        target = 3
    elif snap.turn < 25:
        target = 5
    elif snap.turn < 60:
        target = 10
    elif late or overflow:
        target = max(24, 4 * n_bases + min(60, ledger.gold // 250))
    elif war_prep:
        target = max(16, 3 * n_bases + min(30, ledger.gold // 400))
    elif snap.turn >= 0.6 * snap.max_turns:
        target = min(12, 3 * n_bases + 2)
    else:
        target = min(8, 2 * n_bases + 2)
    if need_defense:
        target = max(target, EARLY_DEFENSE_FLOOR)  # never below the garrison floor
    force = overflow or late or need_defense
    per_turn_cap = 30 if late or overflow else (10 if war_prep else (5 if need_defense else 3))
    cost = UNIT_STATS["Infantry"].gold_cost
    made = 0
    for b in snap.my_buildings:
        if b["type"] != "Barracks" or not b.get("is_complete", True):
            continue
        # don't trickle units into a hopeless siege — bank the gold for the next
        # Base instead (units die one by one against a massed swarm). EXCEPTION:
        # while below the early-defense floor, NOT building is certain death —
        # keep producing or the rush wins uncontested (the turn-30 loss mode).
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
        if not (late or overflow or need_defense) and threat > 3 * (
            ours + UNIT_STATS["Infantry"].attack_power
        ):
            continue
        while (
            alive + pending + made < target
            and made < per_turn_cap
            and ledger.can(cost, force=force)
        ):
            if not _produce(
                snap, mem, ledger, claimed, actions, b, "Infantry", force=force
            ):
                break
            made += 1
        if made >= per_turn_cap:
            break


def _plan_heavy_units(
    snap, mem, ledger, claimed, actions, war_prep=False, late=False, overflow=False
) -> None:
    factories = [
        b for b in snap.my_buildings if b["type"] == "Factory" and b.get("is_complete", True)
    ]
    if not factories:
        return
    artillery = sum(1 for u in snap.my_units if u["type"] == "Artillery")
    artillery += mem.pending_unit_count("Artillery")
    tanks = sum(1 for u in snap.my_units if u["type"] == "Tank")
    tanks += mem.pending_unit_count("Tank")

    if late or overflow:
        artillery_target = max(10, min(36, 4 + ledger.gold // 500))
        tank_target = max(8, min(30, 3 + ledger.gold // 700))
        per_turn_cap = 18
    elif war_prep:
        artillery_target = 6
        tank_target = 4
        per_turn_cap = 8
    elif snap.turn < 90:
        artillery_target = 4
        tank_target = 2
        per_turn_cap = 4
    else:
        artillery_target = 2
        tank_target = 2
        per_turn_cap = 3

    made = 0
    for b in factories:
        while made < per_turn_cap:
            if artillery < artillery_target:
                unit_type = "Artillery"
            elif tanks < tank_target:
                unit_type = "Tank"
            else:
                return
            opening_heavy = snap.turn < 90 and artillery < 4
            use_force = overflow or late or opening_heavy
            if not _produce(
                snap, mem, ledger, claimed, actions, b, unit_type, force=use_force
            ):
                break
            if unit_type == "Artillery":
                artillery += 1
            else:
                tanks += 1
            made += 1


def _plan_air_units(
    snap, mem, ledger, claimed, actions, war_prep=False, late=False, overflow=False
) -> None:
    airbases = [
        b for b in snap.my_buildings if b["type"] == "Airbase" and b.get("is_complete", True)
    ]
    if not airbases:
        return
    fighters = sum(1 for u in snap.my_units if u["type"] == "Fighter")
    fighters += mem.pending_unit_count("Fighter")
    bombers = sum(1 for u in snap.my_units if u["type"] == "Bomber")
    bombers += mem.pending_unit_count("Bomber")

    if late or overflow:
        fighter_target = 6
        bomber_target = 6
        per_turn_cap = 8
    elif war_prep:
        fighter_target = 3
        bomber_target = 2
        per_turn_cap = 3
    else:
        return

    made = 0
    for b in airbases:
        while made < per_turn_cap:
            if fighters < fighter_target:
                unit_type = "Fighter"
            elif bombers < bomber_target:
                unit_type = "Bomber"
            else:
                return
            if not _produce(
                snap, mem, ledger, claimed, actions, b, unit_type, force=overflow or late
            ):
                break
            if unit_type == "Fighter":
                fighters += 1
            else:
                bombers += 1
            made += 1


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
