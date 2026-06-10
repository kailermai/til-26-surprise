# TIL-26 Surprise Context

This file tracks the challenge-specific context for future work on this repo. The README is the main onboarding doc, `participant/RULES.md` is the human rules reference, and `server/src/engine/` is the source of truth for rules and constants. The mirrored engine under `participant/src/engine/` is bundled for submissions and should be treated as read-only.

## Goal

Implement `decide(observation) -> ActionPayload` for a multiplayer free-for-all economic strategy wargame on a toroidal hex grid. Each turn the server calls the participant service at `POST /observe` with a JSON observation and expects an action payload within about 10 seconds. Missing the deadline makes the turn a no-op.

Survival matters: destroying every enemy Base wins, but if the turn limit is reached, every surviving player with a completed Base is a co-winner. Losing the last fully constructed Base permanently eliminates the player; a Base still under construction does not keep the player alive.

## Where To Work

- Edit only under `participant/`.
- Default deterministic agent: `participant/src/algo_agent.py`.
- Optional LLM agent: `participant/src/llm_agent.py`.
- Server wrapper: `participant/src/server.py`; it already exposes `GET /health` and `POST /observe` on port `6700`, so avoid changing it unless necessary.
- Action classes live in `participant/src/engine/actions.py`.
- Full observation/action schema is documented in `participant/RULES.md`.
- Canonical rules/constants are in `server/src/engine/`, especially `server/src/engine/constants.py`.

## Runtime And Submission Constraints

- Local self-test: `docker compose up --build` from repo root.
- Local stage uses 19 `RandomAgent` opponents, 300 turns, 35x30 map, seed `67`.
- Seed `67` is only a stand-in; do not overfit to it.
- Discord eval uses 19 stronger algorithmic bots, 50 turns, hidden seed.
- Real competition uses other teams' agents on an undisclosed predetermined map.
- Stage 1 and real competition enforce about 1 CPU, 1 GiB RAM, and egress only to `openrouter.ai:443`.
- The participant container must respond within about 10 seconds per turn.
- `AGENT` and `OPENROUTER_API_KEY` must be baked into the submitted image; the submit script handles this.
- Submit from Vertex AI Workbench with:

```bash
chmod +777 submit_surprise.sh
./submit_surprise.sh
```

For LLM submission:

```bash
AGENT=llm OPENROUTER_API_KEY=sk-... ./submit_surprise.sh
```

## Core Rules To Remember

- One entity per tile: no stacking, including air over ground or units on buildings.
- All player actions resolve simultaneously and deterministically.
- Phase order is units, then buildings, then coordination.
- Invalid actions are silently dropped.
- Gold is spent up front when an action is accepted; no debt and no refunds.
- Damage is integer damage and all multipliers round down.
- Units can attack and move in the same turn; attacks fire from the pre-move tile before movement.
- A target at distance 0 is invalid; there is no other minimum attack range.
- Dead units are removed before movement resolves, so killed units can free tiles for same-turn movement.
- Build collisions between different players on the same tile make all colliding builds fail with no gold spent.
- A unit moving onto a target build tile can block construction because unit movement resolves before construction.
- A unit moving away can free a tile for construction in the same turn.
- Produced units spawn on the requested adjacent tile if possible; otherwise on any free tile adjacent to the producing building. If all adjacent tiles are occupied when the unit completes, the unit is lost and the spent gold is gone.
- A building that completes this turn does not produce income until the next turn and cannot emit a unit on the same turn it completes.
- Multiple `produce_unit` orders can be queued from one building in the same turn if affordable.

## Map And Economy

- Map is a 35x30 pointy-top hex grid wrapped as a torus.
- Starting state: one completed Base, zero units, 500 gold.
- Starting Base is on a normal tile.
- Terrain distribution is roughly: Normal 45%, Elevated 12%, Difficult 20%, Concealment 13%, Rich Resource 10%.
- Normal, Elevated, Concealment, and Rich Resource cost 1 movement point to enter.
- Difficult terrain costs 2 movement points to enter.
- Elevated attackers deal +25% damage.
- Non-elevated, non-flying observers cannot see past elevated terrain.
- Concealment reduces effective vision into that tile by 1 for non-flying observers; Scouts in concealment are invisible to enemies.
- Completed Base income: 10 gold/turn.
- Completed Mine income: 20 gold/turn.
- Base or Mine on Rich Resource: flat 50 gold/turn instead of normal resource-building income.

## Units

| Unit | HP | Move | Range | Vision | Attack | Cost | Build Turns | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Infantry | 100 | 1 | 1 | 3 | 30 | 50 | 1 | Cheap frontline |
| Scout | 50 | 3 | 1 | 5 | 10 | 100 | 1 | Best vision, fragile |
| Medic | 60 | 1 | 0 | 3 | 0 | 100 | 1 | Passively heals adjacent friendly ground units by 20 HP |
| Tank | 200 | 2 | 1 | 3 | 60 | 200 | 1 | Fast durable raider |
| Artillery | 50 | 1 | 3 | 4 | 60 | 200 | 2 | Splash radius 1 for 50% damage, can fire at empty tiles |
| Fighter | 250 | 3 | 2 | 4 | 50 | 300 | 2 | Flying air superiority |
| Bomber | 150 | 2 | 1 | 3 | 50 | 350 | 3 | Flying, x4 damage vs buildings |

## Buildings

| Building | HP | Cost | Build Turns | Produces | Income | Vision | Notes |
| --- | ---: | ---: | ---: | --- | ---: | ---: | --- |
| Base | 300 | 300 | 5 | None | 10 | +3 | Can be built on any empty visible tile; losing all completed Bases eliminates player |
| Mine | 100 | 200 | 2 | None | 20 | 0 | Must be adjacent to completed own building |
| Barracks | 200 | 100 | 2 | Infantry, Scout, Medic | 0 | 0 | Must be adjacent to completed own building |
| Factory | 200 | 300 | 3 | Tank, Artillery | 0 | 0 | Must be adjacent to completed own building |
| Airbase | 200 | 500 | 3 | Fighter, Bomber | 0 | 0 | Must be adjacent to completed own building |

## Observation Notes

Top-level observation fields include `player_id`, `turn_number`, `max_turns`, `map_width`, `map_height`, `resources`, `visible_tiles`, `treaties`, `incoming_treaty_proposals`, `known_players`, `global_chat`, and `private_chat`.

Only currently visible tiles appear. There is no built-in memory of past sightings. Unit-production queues are not included in observations, so any agent that cares about pending spawns must track its own production orders across turns.

## Diplomacy And Chat

- Peace treaties prevent primary-target attack damage between treaty partners.
- Artillery splash ignores ownership and treaties, so it can damage self and allies.
- Treaties take 5 turns to break and stay active during the break countdown.
- From turn 200 onward, all treaties are voided and no new treaties can form.
- DMs and treaty proposals require the target to be in `known_players`.
- Global chat is always open.
- Chat history is uncapped in observations, so robust agents should avoid expensive full-history processing.

## Testing And Replay

Run the local match:

```bash
docker compose up --build
```

Run the LLM agent locally:

```bash
AGENT=llm OPENROUTER_API_KEY=sk-... docker compose up --build
```

Replays are written to `./replays/`. Optional viewer setup:

```bash
python -m venv env
. env/bin/activate
pip install -r server/requirements-viewer.txt
python server/src/watch_replay.py
```
