# Recommended Approach

Use a hybrid agent, but make deterministic Python the primary player. The LLM should be an optional high-level strategist, not the system responsible for legal moves, targeting, pathing, or turn-by-turn combat.

The game rewards reliable execution under tight constraints: about 10 seconds per turn, 1 CPU, 1 GiB RAM, toroidal hex geometry, hidden maps, and silent dropping of invalid actions. A stable deterministic agent that always emits legal useful actions is more valuable than a clever LLM agent that sometimes times out or makes coordinate mistakes.

## Main Principle

Do not ask the LLM to produce exact game actions like move paths, attack coordinates, construction coordinates, or unit spawn tiles.

Instead, let Python compute the actual `ActionPayload`. If an LLM is used, it should only return compact strategic preferences that Python can validate and translate.

## Deterministic Python Responsibilities

Python should handle all mechanics-sensitive work:

- Observation parsing into own units, own buildings, visible enemies, terrain, resources, and occupied tiles.
- Hex distance, torus wrapping, neighbor generation, movement costs, and pathfinding.
- Attack range checks and target selection.
- Movement decisions and collision avoidance.
- Build and production affordability.
- Choosing valid construction and spawn tiles.
- Combat tactics:
  - focus fire killable enemies;
  - prioritize enemy Bases and production buildings;
  - avoid artillery splash on own units;
  - attack before moving where useful;
  - defend own Base when enemies are close.
- Economy and production:
  - early Barracks/Mine setup;
  - Scout production for map knowledge;
  - Infantry for cheap bodies;
  - Factory and Artillery/Tanks once economy is stable;
  - Airbase/Fighters/Bombers only when gold income supports it.
- Persistent memory:
  - last seen enemy positions;
  - known enemy Bases;
  - known rich-resource tiles;
  - own pending production if needed;
  - explored/unexplored map regions;
  - players met through vision/chat/diplomacy.

## LLM Responsibilities

If used, the LLM should only make high-level decisions such as:

- Strategic mode:
  - `expand`
  - `defend`
  - `rush`
  - `tech`
  - `economy`
  - `hunt_base`
- Target preference:
  - which known enemy player to pressure;
  - whether to avoid a nearby player temporarily;
  - whether a discovered enemy Base should become the main objective.
- Diplomacy:
  - accept or reject peace proposals;
  - propose peace to a known nearby player;
  - decide when to break a treaty before the turn-200 cutoff.
- Chat:
  - send short global or private messages;
  - coordinate simple diplomacy;
  - never rely on chat for core survival.

The LLM should be called sparingly: every 5-10 turns, or only on major events such as first enemy contact, own Base under threat, treaty proposal received, enemy Base discovered, large gold surplus, or approaching turn 200.

Always cache the most recent LLM strategy. If the LLM times out, fails JSON parsing, or returns unusable advice, keep using the cached strategy or a deterministic default.

## Suggested Architecture

```text
observation
  -> StateTracker updates memory
  -> FeatureExtractor creates compact strategic summary
  -> optional LLMStrategist updates high-level plan
  -> EconomyPlanner chooses buildings and production
  -> TacticalEngine chooses attacks and movement
  -> ActionValidator/Translator builds ActionPayload
```

The deterministic planners should be able to run without the LLM. The LLM layer should only influence priorities, not legality.

## Suggested LLM Output Schema

Use a small schema like this:

```json
{
  "mode": "expand",
  "focus_player": "player-8",
  "preferred_unit_mix": ["Scout", "Infantry", "Artillery"],
  "diplomacy": [
    {"action": "accept_peace", "player_id": "player-4"}
  ],
  "chat": [
    {"recipient_id": null, "text": "Open to peace while we scout."}
  ],
  "notes": "Prioritize a second Base near rich resources and avoid overextending tanks."
}
```

Python should treat this as advice. It must still check whether the named player is known, whether diplomacy is legal, whether turn 200 has passed, whether the unit mix is affordable, and whether any suggested priority is tactically sensible.

## What Not To Put In The LLM

Avoid LLM control over:

- Exact `MoveAction` paths.
- Exact `AttackAction` target coordinates.
- Exact `ConstructBuildingAction` coordinates.
- Exact `ProduceUnitAction` spawn coordinates.
- Hex distance calculations.
- Whether a tile is occupied.
- Whether an action is affordable.
- Full chat-history interpretation every turn.

These are deterministic, fragile, and easy for Python to do better.

## Initial Implementation Plan

1. Build a strong pure `algo_agent.py` first.
2. Add robust observation parsing and persistent memory.
3. Improve economy:
   - build early Barracks and Mines;
   - produce Scouts for exploration;
   - add Factory once income is stable;
   - scale into Artillery/Tanks, then air only if affordable.
4. Improve tactics:
   - focus fire;
   - prioritize enemy Bases/buildings;
   - defend own Base;
   - move Scouts to unexplored/rich-resource/frontier tiles;
   - keep production buildings from being boxed in.
5. Run local matches and inspect replays.
6. Only after the deterministic baseline is reliable, add optional LLM strategy for diplomacy and high-level mode selection.

## Bottom Line

The agent should be an algorithmic player with an optional strategic advisor. The LLM can help choose intent, diplomacy, and broad priorities, but Python must own all game mechanics and final action construction.
