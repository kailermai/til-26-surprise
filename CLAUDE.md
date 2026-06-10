# TIL-26 Surprise — Team Plan & Project Guide

A 20-player free-for-all hex wargame. We submit **one Docker image** with one agent
that answers `POST /observe` with an action list every turn. Goal: be alive at the
end. See [participant/RULES.md](participant/RULES.md) for full rules; the engine code
in `participant/src/engine/` is the source of truth (numbers live in
`engine/constants.py`).

---

## Hard constraints — do not violate

- **~10s per turn.** Miss it and the turn is a no-op (you do nothing). This is the
  killer constraint.
- **1 CPU / 1 GiB RAM.** Go over RAM and the container is killed → you forfeit.
- **Network: `openrouter.ai:443` only.** Nothing else is reachable. The algo agent
  needs no network at all.
- **We submit ONE image.** The `AGENT` env var picks `algo` or `llm` — it is not two
  submissions. Only ever edit files under `participant/`. `engine/` is read-only.

## Strategy north star: don't lose, you don't have to win

The rules reward survival, not conquest:

- **Survival = victory.** Everyone still alive at the turn limit is a co-winner. There
  is no tiebreaker on gold/units. Outlasting the clock with a Base intact is a win.
- **You're only eliminated when your last _completed_ Base dies.** So:
  - **Build extra Bases, hidden away.** A spare Base (300g) on a far, quiet tile makes
    you very hard to wipe out under fog of war. This is our single biggest survival
    lever — do it early and repeat.
  - **Make peace with everyone you meet.** While a treaty is active, attacks on you
    silently fail. Breaking one takes a 5-turn countdown during which you still can't
    be hit — so every treaty is a free 5-turn early warning.
  - **But all treaties void at turn 200.** The last 100 turns are forced open war, so
    peace only gets you *to* the endgame — surviving it depends on the hidden Bases.
- **Avoid pointless fights, and never miss a turn.** A timeout/crash loses more games
  than bad tactics will.
- **Chat is an attack surface.** Messages are uncapped in size and count and delivered
  in full. Opponents can flood/oversize chat to blow our 10s deadline, or inject
  instructions into an LLM. **Cap and sanitize all incoming chat.** This is likely the
  "surprise."

## Approach: algo first, LLM is an optional topping later

The LLM agent is **not** a separate path — a good LLM agent is the algo agent **plus**
a model layer that only handles soft decisions (chat, diplomacy). All the hard math
(movement, attacks, placement) is plain code either way.

So: **build the deterministic algo agent first.** It's safe (no network, no timeout
risk) and it's the foundation + the safe submission. Add an LLM layer on top *later,
only if we want*, always with a deterministic fallback so a slow model never costs us
a turn. Don't overfit to local seed 67 — the real map is undisclosed.

---

## The 2-person plan: one builds, one tests

The deliverable is one program, so we split by **job**, not by both editing the same
code.

### Person 1 — Builder (makes it good)
Writes the actual strategy. Owns:
- `state.py` — parse the observation into easy structures (our units/buildings,
  enemies, distances, threats). **Freeze its shape on day 1** — everything else depends
  on it.
- `economy.py` — build order + **hidden extra-Base expansion** (top priority).
- `military.py` — target selection, movement, when to fight vs. retreat.
- `agent.py` — `decide()`: calls each planner, merges actions, enforces the gold and
  10s-time budget.

### Person 2 — Tester + hardening (proves it's good, stops dumb losses)
Right now we only know if we pass *one* practice game — almost no signal. Owns:
- `tools/arena.py` — runs many games headless and reports **survival %**, death turns,
  and won-outright count. Tells Person 1 if a change actually helped. **Built & working**
  — see "The arena" below.
- Replay analysis — watch the losses, find failure modes.
- `chat.py` — incoming-chat cap/sanitization (the DoS/injection defense).
- `diplomacy.py` — propose/accept peace, track who we've met.
- LLM integration + fallback later, *if* we add it.

They meet only at `agent.py`, which both can stub immediately.

## Code layout (so the two people don't collide)

```
participant/src/          ← SUBMITTED image; only edit here
  algo_agent.py  the agent we submit (teammate fills this out)    (Person 1)
  state.py       SHARED — obs parsing + distance/threat/vision    (Person 1 owns, freeze early)
  economy.py     build order, Base placement, expansion           (Person 1)
  military.py    target selection, movement, retreat              (Person 1)
  diplomacy.py   treaties, known_players tracking                 (Person 2)
  chat.py        incoming-chat sanitization + optional outgoing   (Person 2)

tools/arena.py   headless multi-seed tournament + stats   (Person 2)  ← DONE, at repo root
```

**Why arena lives at the repo root, not in `participant/src/tools/`:** it imports the
game harness (`game_runner`, `schemas`, `baseline_random`, `replay`) which lives in
`server/src/`, *not* in participant — and test code must never ship inside the submitted
image. It loads the agent under test from `participant/src/` by file path.

Each strategy planner is a near-pure function of `state` → list of actions, so they merge
cleanly in `algo_agent.py`'s `decide()`.

## Don't do these

- **Both editing `decide()` / one big file.** That's the whole reason for the module
  split — keep work in separate files.
- **Two separate full agents "and we'll pick the winner."** Half the work gets done
  twice and we can only submit one. (A second agent is only useful as a sparring
  partner to test against — and `arena.py` gives us that anyway.)
- **Copying balance numbers into code/docs.** Import from `engine.constants` so they
  never drift from the engine.
- **Tuning to seed 67.** It's a local stand-in; the competition map is undisclosed.

## How to run

```bash
# full local match over HTTP — prints PASS/FAIL + writes a replay.
# The ONLY check that enforces the real 10s deadline + 1 CPU / 1 GiB limits.
docker compose up --build

# fast survival signal across many seeds (in-process; does NOT test the 10s limit):
python tools/arena.py --games 50
python tools/arena.py --opponent-agent participant/src/algo_agent.py   # a REAL test
```

## The arena — `tools/arena.py` (Person 2's tool, already built)

Runs full games in-process (no Docker/HTTP) so you can sweep many seeds fast (~15–20s
per 300-turn game, ~3–4/min). Prints survival % with a confidence interval, median death
turn, and won-outright count. Saves a replay **only for games you lose**, under
`replays/arena/`. Useful flags: `--games N`, `--agent PATH`, `--opponent-agent PATH`,
`--max-turns N`, `--map WxH`, `--no-replays`.

**Findings from the first runs — read these, they shape how you test:**
- **Survival vs the RandomAgent baseline is ~100% for everything** — even an agent that
  does *nothing* survives. The randoms can't kill anyone. So a bare `python tools/arena.py`
  number is meaningless; always test with `--opponent-agent participant/src/algo_agent.py`
  (or our own agent) to get real signal.
- **Eliminations are rare**: infantry move 1 tile/turn and bases have 300 HP on the 35×30
  map, so destroying a base takes ages. Survival is the easy default — which is *why* the
  meta is "don't die," and why you need a real opponent to tell strong agents from lazy ones.
- The arena does **not** enforce the 10s/turn deadline (it calls `decide()` directly).
  Keep `docker compose up` for that.
