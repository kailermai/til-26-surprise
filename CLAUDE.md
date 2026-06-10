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
- `tools/arena.py` — run ~30 games on random seeds headless, report **survival %**
  and which turn/why we died. This tells Person 1 if a change actually helped.
- Replay analysis — watch the losses, find failure modes.
- `chat.py` — incoming-chat cap/sanitization (the DoS/injection defense).
- `diplomacy.py` — propose/accept peace, track who we've met.
- LLM integration + fallback later, *if* we add it.

They meet only at `agent.py`, which both can stub immediately.

## Code layout (so the two people don't collide)

```
participant/src/
  state.py       SHARED — obs parsing + distance/threat/vision helpers (Person 1 owns, freeze early)
  economy.py     build order, Base placement, expansion          (Person 1)
  military.py    target selection, movement, retreat              (Person 1)
  agent.py       decide(): wires planners together, budgets time  (Person 1)
  diplomacy.py   treaties, known_players tracking                 (Person 2)
  chat.py        incoming-chat sanitization + optional outgoing   (Person 2)
  tools/arena.py headless multi-seed tournament + stats           (Person 2)
```

Each planner is a near-pure function of `state` → list of actions, so they merge
cleanly in `agent.py`.

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
# from til-26-surprise/ — full local match, prints PASS/FAIL + writes a replay
docker compose up --build

# once Person 2 builds it: fast signal across many seeds
python participant/src/tools/arena.py
```
