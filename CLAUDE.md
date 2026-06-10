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
  agent.py       MainAgent — the agent we ACTUALLY submit; wires the planners
                 under the time budget (server.py loads this)     (Person 1)
  algo_agent.py  the UNTOUCHED starter template — server.py's fallback if
                 MainAgent fails to import; useful only as a weak sparring partner
  state.py       SHARED — obs parsing + distance/threat/vision    (Person 1 owns, freeze early)
  economy.py     build order, Base placement, expansion           (Person 1)
  military.py    target selection, movement, retreat              (Person 1)
  diplomacy.py   treaties, known_players tracking                 (Person 2)
  chat.py        incoming-chat sanitization + optional outgoing   (Person 2)

tools/arena.py            headless multi-seed tournament + stats   (Person 2)  ← DONE, at repo root
tools/arena_parallel.py   same tournament sharded across all CPU cores (same flags + --workers)
```

**Why arena lives at the repo root, not in `participant/src/tools/`:** it imports the
game harness (`game_runner`, `schemas`, `baseline_random`, `replay`) which lives in
`server/src/`, *not* in participant — and test code must never ship inside the submitted
image. It loads the agent under test from `participant/src/` by file path.

Each strategy planner is a near-pure function of `state` → list of actions, so they merge
cleanly in `agent.py`'s `decide()`.

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

> **Which file is "the agent"?** Our real agent is **`MainAgent` in
> `participant/src/agent.py`** — server.py loads it for Docker/submission (falling back
> to the template only if its import fails), and it is the arena's default `--agent`.
> `participant/src/algo_agent.py` is the **untouched starter template** — only useful
> as a weak sparring partner. The arena header prints which file it loaded
> (`agent=agent.py`) — glance at it before trusting any numbers.

```bash
# full local match over HTTP — prints PASS/FAIL + writes a replay.
# The ONLY check that enforces the real 10s deadline + 1 CPU / 1 GiB limits.
docker compose up --build

# fast survival signal across many seeds (in-process; does NOT test the 10s limit).
# Defaults to our real agent (agent.py) vs 19 RandomAgents:
python tools/arena.py --games 50
# the REAL test — self-play, all 20 players run our agent:
python tools/arena.py --games 20 --opponent-agent participant/src/agent.py

# UNCONFOUNDED A/B — fixed sparring opponent (self-play changes all 19 opponents
# whenever we change our agent; HunterBot stays constant between runs). It never
# makes peace, scouts the map for our hidden Bases, and sieges with Artillery:
python tools/arena.py --games 14 --opponent-agent tools/hunter_bot.py
# (FREEZE RULE: never tune hunter_bot.py to be beatable — make hunter_bot_v2.py)

# the arena is single-threaded (~30s/game at 300 turns) — for sweeps use the
# parallel front-end, which shards seeds across CPU cores (~6-7x on a Ryzen 7,
# identical seeds/stats, same flags plus --workers):
python tools/arena_parallel.py --games 50 --opponent-agent participant/src/agent.py

# faster still while iterating: Discord-eval length + skip replay writes
# (OneDrive sync makes replay writes slow on our laptops):
python tools/arena_parallel.py --games 50 --max-turns 50 --no-replays --opponent-agent participant/src/agent.py
```

Windows note: if `python` hits `ModuleNotFoundError: httpx`, the terminal is resolving
to an interpreter without it — use the full path `C:\Python313\python.exe` (or
`pip install httpx` into whatever interpreter you're running).

## The arena — `tools/arena.py` (Person 2's tool, already built)

Runs full games in-process (no Docker/HTTP) so you can sweep many seeds fast (~15–20s
per 300-turn game, ~3–4/min). Prints survival % with a confidence interval, median death
turn, and won-outright count. Saves a replay **only for games you lose**, under
`replays/arena/`. Useful flags: `--games N`, `--agent PATH`, `--opponent-agent PATH`,
`--max-turns N`, `--map WxH`, `--no-replays`.

**Findings from the first runs — read these, they shape how you test:**
- **The default `--agent` is our real agent** (`agent.py` / MainAgent — changed from the
  template on 2026-06-10). The arena header prints which file it loaded; check it says
  `agent=agent.py` before trusting a run.
- **Survival vs the RandomAgent baseline is ~100% for everything** — even an agent that
  does *nothing* survives. The randoms can't kill anyone. So an arena run without
  `--opponent-agent` is meaningless; spar against our own agent
  (`--opponent-agent participant/src/agent.py`) to get real signal.
- **Eliminations are rare**: infantry move 1 tile/turn and bases have 300 HP on the 35×30
  map, so destroying a base takes ages. Survival is the easy default — which is *why* the
  meta is "don't die," and why you need a real opponent to tell strong agents from lazy ones.
- The arena does **not** enforce the 10s/turn deadline (it calls `decide()` directly).
  Keep `docker compose up` for that.

## Loss analysis — `tools/postmortem.py`

Explains *why* a replay was lost: every Base's lifecycle (where, founded/completed/
destroyed turn, and **who** destroyed it from the action logs), plus what we held at
death (gold, units, buildings) and how far apart the bases were.

```bash
python tools/postmortem.py replays/arena/seed_6.jsonl
python tools/postmortem.py replays/arena/seed_6.jsonl --player player-3   # autopsy anyone
```

## ⚠️ Open investigation — VERIFY before trusting, do not take on faith

These are findings from arena sweeps + post-mortems. **Re-run the commands and confirm
the numbers yourself before acting** — they are a snapshot of one set of seeds, the
agent code changes underneath this note, and small-n survival % has wide error bars.
Standard repro (14 seeds, comparable across versions):
```bash
python tools/arena_parallel.py --games 14 --opponent-agent participant/src/agent.py
python tools/postmortem.py replays/arena/seed_6.jsonl   # then 8, 9, 2, 13, ...
```

### Version history (self-play, seeds 1–14, same 14 seeds each time)
- **v1**: 9/14 (64%), deaths ~236–295, **~13 survivors/game**. Dies on ~24–28k gold, 1–2 Bases.
- **v2**: 7/14 (50%), deaths ~209–255. "Dump gold" caps + pickier base siting —
  *regressed* (a `base_site_threat>=8` gate made some seeds build only ONE Base).
- **v3**: 9/14 (64%), deaths ~221–246. Opportunistic founding + more Scouts.
  **Base count fixed**: 12–26 Bases/game (was 1–2). But still died too-late + no defenders.
- **v5**: 4/14 (29%), deaths ~216–300, **~5.5 survivors/game**. Full tech tree + whole unit
  roster + offensive doctrine. **Read this number carefully — see "v5 assessment" below: the
  survival drop is mostly the meta turning violent, NOT proof v5 is worse.**
- n=14 CI is ±~20 pts. **Raw self-play % across versions is NOT a clean comparison** — the
  opponents change with our agent (see the confound note below), so a lower number can mean
  "we made the board deadlier," not "we got worse." Trend-watching survival % alone is a trap.

### v3 RESOLVED two earlier findings (kept for the record)
- *Dies rich* + *base count gated by scout vision, not gold* → fixed. v3 founds a Base on
  any visible tile clearing a quality bar, and runs 3–4 Scouts. Post-mortems now show
  12–26 Bases/game and gold drained to single digits-k in several losses. ✅
- *v2 `base_site_threat>=8` gate regressed base-founding* → confirmed and removed in v3
  (threat distance now relaxes over time: 8→6→4). ✅

### v4/v5 RESOLVED the two findings above (kept for the record)
v5 post-mortems (seeds 8/11/2) confirm both v4 targets were hit:
- *Bases founded too late* → fixed. `founded t…` now spreads across peacetime (t0, t12, t62,
  t101, t141…) instead of bunching at t181+. `WAR_PREP_TURN = TREATY_CUTOFF−60` (turn 140). ✅
- *Zero defenders / dies rich* → fixed. v5 builds the full tech tree + roster and dies on
  **90–160 gold** with Infantry/Artillery/Fighters present (was `units:{}` on 27k gold). ✅

### v5 assessment — survival LOOKS worse (29%) but is mostly confounded
Do NOT revert v5 on the 64%→29% number alone. The post-mortems tell a different story:
- **The board became a bloodbath.** Survivors/game fell ~13 (v1/v3) → ~5.5 (v5) because v5's
  offensive doctrine made all 20 self-play copies aggressive. Lower survival is largely the
  *meta* getting deadlier, not our agent getting worse — self-play can't separate the two.
- **Real remaining weaknesses (these ARE worth fixing):**
  1. **Defenders too dispersed to hold.** v5 has an army but the offense marches it AWAY to
     enemy Bases, so each home Base is thinly held and breaks to coordinated Tank+Fighter+
     Artillery swarms. Concentrate force; keep a real garrison floor home.
  2. **Offense is strategically suspect for THIS game.** Win condition = "don't die"; kills
     earn nothing. Marching out both under-defends home and (in self-play) makes the board
     deadlier. A concentrated turtle likely beats a disperser. A/B it (see hunter-bot below).
  3. **Post-200 Base spam still wasteful.** Bases founded t210+ mostly show "never completed"
     (razed mid-build, 300g each wasted). The *peacetime* Bases are the good ones.

### Submission status
- **Discord eval: WIN** (algo agent, 0/50 errored turns, 2026-06-10). NOTE: Discord runs only
  **50 turns**, and our deaths all happen post-200 — so a Discord WIN mainly proves "runs clean
  / no crash / no timeout vs real bots," NOT endgame survival. Still the safe locked-in baseline.
- Opponents differ per stage (README): local `docker compose` = 19 **RandomAgents** (can't kill,
  ~100% survival for anything); Discord = 19 **stronger algo bots**; real comp = other teams.

### How to improve from here (ranked by leverage)
1. **Build a fixed "hunter" sparring bot** (single self-contained file like `algo_agent.py`, so
   it doesn't collide with our agent's modules — see testing limit below). Scouts aggressively,
   beelines for Bases, concentrates force. This is the #1 gap: it gives **unconfounded** signal
   ("did survival vs the hunter go up?"), which self-play cannot. Unblocks all other tuning.
2. **A/B turtle vs offense** against that hunter — settle whether v5's offensive doctrine helps
   or hurts survival.
3. **Stress-test the chat DoS (the likely "surprise").** We sanitize (`chat.py` caps msgs/len,
   planners never read chat text), but VERIFY it: feed `decide()` an obs with ~10k oversized
   chat messages and measure the time. Flat = defense holds; spikes = a real hole to plug.
4. **Harden timing** — see below.

### ⏱ decide() time is creeping — Docker check is now overdue
Slowest `decide()`: v1 ~0.8s → v2 ~1.3s → v3 ~1.9s → **v5 ~2.25s** in 300-turn self-play (more
buildings + a big army = heavier per-turn loops). Under 10s in-process, BUT the arena runs on a
fast laptop core with **no cap** — under Docker's real **1-CPU / 1 GiB** limit this can be 2–3×
higher (~5–6s), approaching the wall. A timeout = no-op turn = a free death. **Confirm with
`docker compose up --build`** (the only test that enforces the real deadline) before trusting v5
is submission-safe. If tight, budget-cap the Base/unit loops the way A* is rationed in `military.py`.

### ⚠️ Testing limit: you CANNOT cleanly A/B two versions of our agent in the arena
Our agent is multi-file (`agent.py` imports `economy`/`military`/`state`/…). Python caches modules
by name in `sys.modules`, so loading a *second* multi-file agent in the same arena process reuses
the FIRST one's `economy`/`military` — a "v5 vs 19×v3" game actually runs v5 vs ~v5. So:
- Version A/B via `--opponent-agent participant/src/agent.py` against an OLD checkout does **not**
  isolate the change. The only clean isolation is **each version in its own process vs a fixed,
  single-file opponent** (the hunter bot or `algo_agent.py`).
- This is also why the real verdict comes from **Discord / the real competition**, not self-play.
