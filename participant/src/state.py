"""SHARED observation parsing + persistent memory. No decisions live here.

`parse()` turns the raw observation dict into a `Snapshot` (this turn only);
`update()` folds the snapshot into the long-lived `Memory` that survives across
turns (the observation has no history — no past sightings, no production queue).
Planners (`economy`, `military`, `diplomacy`) read both and never re-parse.

Freeze this module's shape early: everything else depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.constants import BUILDING_STATS
from engine.hex_grid import HexCoord, HexGrid

Coord = tuple[int, int]  # canonical wrapped (q, r) — hashable, JSON-friendly

ENEMY_MEMORY_TURNS = 25  # forget a last-seen enemy unit after this many turns
THREAT_FRESH_TURNS = 10  # only sightings this recent count toward threat


# ── gold ledger ───────────────────────────────────────────────────────────────


class Ledger:
    """Shared gold budget so planners can't double-spend. `reserve()` earmarks
    gold (e.g. saving for a Base); `spend(force=True)` may dip into the reserve
    for purchases that are themselves survival-critical."""

    def __init__(self, gold: int) -> None:
        self.gold = gold
        self.reserved = 0

    def available(self, force: bool = False) -> int:
        return self.gold if force else self.gold - self.reserved

    def can(self, cost: int, force: bool = False) -> bool:
        return cost <= self.available(force)

    def spend(self, cost: int, force: bool = False) -> bool:
        if not self.can(cost, force):
            return False
        self.gold -= cost
        if force:
            self.reserved = min(self.reserved, self.gold)
        return True

    def reserve(self, amount: int) -> None:
        self.reserved = min(self.gold, self.reserved + amount)

    def release(self, amount: int) -> None:
        self.reserved = max(0, self.reserved - amount)


# ── per-turn snapshot ─────────────────────────────────────────────────────────


@dataclass
class Snapshot:
    pid: str
    turn: int
    max_turns: int
    gold: int
    grid: HexGrid
    terrain: dict[Coord, str]
    visible: set[Coord]
    occupied: set[Coord]
    my_units: list[dict]
    my_buildings: list[dict]
    my_bases_done: list[dict]
    my_bases_building: list[dict]
    enemy_units: list[dict]
    enemy_buildings: list[dict]
    treaties: dict[str, dict]  # partner_id -> {"treaty_type", "breaking_in_turns"}
    proposals: list[dict]  # [{"proposer_id", "treaty_type"}]
    known_players: list[str]

    def peace_active(self, owner_id: str) -> bool:
        """True only for a STABLE treaty. A treaty in its break countdown no
        longer blocks attacks (engine gates on ACTIVE status only), so a
        breaking partner is hostile NOW."""
        t = self.treaties.get(owner_id)
        return t is not None and t.get("breaking_in_turns") is None

    def hostile(self, owner_id: str) -> bool:
        return not self.peace_active(owner_id)

    def hostile_units(self) -> list[dict]:
        return [u for u in self.enemy_units if self.hostile(u["owner_id"])]

    def main_base(self) -> dict | None:
        if self.my_bases_done:
            return self.my_bases_done[0]
        if self.my_bases_building:
            return self.my_bases_building[0]
        return None


def parse(obs: dict) -> Snapshot:
    pid = obs["player_id"]
    grid = HexGrid(obs.get("map_width", 35), obs.get("map_height", 30))

    terrain: dict[Coord, str] = {}
    visible: set[Coord] = set()
    occupied: set[Coord] = set()
    my_units: list[dict] = []
    my_buildings: list[dict] = []
    enemy_units: list[dict] = []
    enemy_buildings: list[dict] = []

    for tile in obs.get("visible_tiles", []):
        c = (tile["q"], tile["r"])
        visible.add(c)
        terrain[c] = tile.get("terrain", "normal")
        for e in tile.get("entities", []):
            occupied.add((e["q"], e["r"]))
            is_building = e.get("type") in BUILDING_STATS
            if e.get("owner_id") == pid:
                (my_buildings if is_building else my_units).append(e)
            else:
                (enemy_buildings if is_building else enemy_units).append(e)

    bases = [b for b in my_buildings if b["type"] == "Base"]
    my_bases_done = [b for b in bases if b.get("is_complete", True)]
    my_bases_building = [b for b in bases if not b.get("is_complete", True)]

    treaties = {
        t["partner_id"]: t
        for t in obs.get("treaties", [])
        if isinstance(t, dict) and t.get("partner_id")
    }
    proposals = [
        p for p in obs.get("incoming_treaty_proposals", [])
        if isinstance(p, dict) and p.get("proposer_id")
    ]

    return Snapshot(
        pid=pid,
        turn=obs.get("turn_number", 0),
        max_turns=obs.get("max_turns", 300),
        gold=obs.get("resources", {}).get("gold", 0),
        grid=grid,
        terrain=terrain,
        visible=visible,
        occupied=occupied,
        my_units=my_units,
        my_buildings=my_buildings,
        my_bases_done=my_bases_done,
        my_bases_building=my_bases_building,
        enemy_units=enemy_units,
        enemy_buildings=enemy_buildings,
        treaties=treaties,
        proposals=proposals,
        known_players=[p for p in obs.get("known_players", []) if isinstance(p, str)],
    )


# ── cross-turn memory ─────────────────────────────────────────────────────────


class Memory:
    """Everything we must remember ourselves: explored terrain, last-seen
    enemies, known enemy Bases, our own pending builds/production, and who has
    ever turned on us. Lives on the agent instance."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.last_turn = -1
        self.terrain_map: dict[Coord, str] = {}
        self.explored: set[Coord] = set()
        self.rich_tiles: set[Coord] = set()
        self.last_seen_enemy: dict[str, dict] = {}  # id -> {coord,turn,owner,type,hp,...}
        self.known_enemy_bases: dict[str, dict] = {}  # id -> {coord,owner,last_seen}
        self.pending_builds: dict[Coord, dict] = {}  # coord -> {type, turn}
        self.pending_production: dict[str, list[dict]] = {}  # bld_id -> [{unit_type, due}]
        self.aggressors: set[str] = set()
        self.base_site_target: Coord | None = None
        self.base_site_turn = -99  # when the site was last (re)chosen
        self.scout_goals: dict[str, Coord] = {}
        self.last_proposed: dict[str, int] = {}
        self.greeted: set[str] = set()
        self.freeze_scouts_turn = -1  # don't move scouts the turn we place a Base
        self._my_hp: dict[str, dict] = {}  # id -> {hp, coord}

    # -- update ---------------------------------------------------------------

    def update(self, snap: Snapshot) -> None:
        if snap.turn < self.last_turn:
            self.reset()  # new game reused this instance

        # cumulative map knowledge
        for c, t in snap.terrain.items():
            self.terrain_map[c] = t
            if t == "rich_resource":
                self.rich_tiles.add(c)
        self.explored |= snap.visible

        # enemy sightings
        present: set[str] = set()
        for e in snap.enemy_units + snap.enemy_buildings:
            present.add(e["id"])
            self.last_seen_enemy[e["id"]] = {
                "coord": (e["q"], e["r"]),
                "turn": snap.turn,
                "owner": e["owner_id"],
                "type": e["type"],
                "hp": e.get("hp", 0),
                "attack_power": e.get("attack_power", 0),
                "attack_range": e.get("attack_range", 0),
            }
            if e["type"] == "Base":
                self.known_enemy_bases[e["id"]] = {
                    "coord": (e["q"], e["r"]),
                    "owner": e["owner_id"],
                    "last_seen": snap.turn,
                    "is_complete": e.get("is_complete", True),
                }
        # prune: a remembered entity whose tile we can see but who isn't there
        # anymore has moved or died; old unit sightings age out entirely.
        for eid in list(self.last_seen_enemy):
            info = self.last_seen_enemy[eid]
            gone = eid not in present and info["coord"] in snap.visible
            stale = snap.turn - info["turn"] > ENEMY_MEMORY_TURNS
            if gone or (stale and info["type"] not in BUILDING_STATS):
                del self.last_seen_enemy[eid]
        for eid in list(self.known_enemy_bases):
            info = self.known_enemy_bases[eid]
            if eid not in present and info["coord"] in snap.visible:
                del self.known_enemy_bases[eid]

        # aggressors: a treaty flipping into its break countdown is war now
        for partner, t in snap.treaties.items():
            if t.get("breaking_in_turns") is not None:
                self.aggressors.add(partner)
        # ... and anyone plausibly responsible for damage we just took
        damaged: list[Coord] = []
        for e in snap.my_units + snap.my_buildings:
            prev = self._my_hp.get(e["id"])
            if prev is not None and e.get("hp", 0) < prev["hp"]:
                damaged.append((e["q"], e["r"]))
        if damaged:
            for u in snap.enemy_units:
                reach = u.get("attack_range", 0) + 1
                uc = HexCoord(u["q"], u["r"])
                for dc in damaged:
                    if snap.grid.distance(uc, HexCoord(*dc)) <= reach:
                        self.aggressors.add(u["owner_id"])
                        break
        self._my_hp = {
            e["id"]: {"hp": e.get("hp", 0)}
            for e in snap.my_units + snap.my_buildings
        }

        # our pending builds: success (our building stands there) or visible
        # failure / timeout clears the entry
        mine_at: set[Coord] = {(b["q"], b["r"]) for b in snap.my_buildings}
        for c in list(self.pending_builds):
            info = self.pending_builds[c]
            if c in mine_at or snap.turn - info["turn"] >= 3:
                del self.pending_builds[c]

        # pending production: drop entries past their due turn
        for bid in list(self.pending_production):
            queue = [q for q in self.pending_production[bid] if q["due"] > snap.turn]
            if queue:
                self.pending_production[bid] = queue
            else:
                del self.pending_production[bid]

        self.last_turn = snap.turn

    # -- queries ----------------------------------------------------------------

    def pending_unit_count(self, unit_type: str) -> int:
        return sum(
            1
            for queue in self.pending_production.values()
            for q in queue
            if q["unit_type"] == unit_type
        )

    def pending_build_count(self, building_type: str) -> int:
        return sum(1 for i in self.pending_builds.values() if i["type"] == building_type)

    def fresh_threats(self, snap: Snapshot) -> list[dict]:
        """Recently seen hostile units (visible now or seen very recently)."""
        out = []
        for info in self.last_seen_enemy.values():
            if info["type"] in BUILDING_STATS:
                continue
            if snap.turn - info["turn"] > THREAT_FRESH_TURNS:
                continue
            if snap.hostile(info["owner"]):
                out.append(info)
        return out

    def threat_near(self, snap: Snapshot, coord: Coord, radius: int) -> int:
        """Total attack power of fresh hostile units within `radius`."""
        center = HexCoord(*coord)
        total = 0
        for info in self.fresh_threats(snap):
            if snap.grid.distance(center, HexCoord(*info["coord"])) <= radius:
                total += info.get("attack_power", 0)
        return total


def update(mem: Memory, snap: Snapshot) -> None:
    mem.update(snap)
