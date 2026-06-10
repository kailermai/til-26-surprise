"""Arena — fast, in-process tournament harness for measuring agent strength.

WHY THIS EXISTS
---------------
`docker compose up` runs ONE game (seed 67) over HTTP and prints PASS/FAIL — almost
no signal. The arena instead runs the WHOLE game as plain Python in memory (the 19
opponents already run in-process in the official harness; here YOUR agent does too),
so you can sweep many seeds unattended. A full 300-turn 20-player game is ~15-20s of
simulation (the engine, not the agent), i.e. ~3-4 games/min; a 100-game sweep is
~half an hour. Use --games 20 for a quick check, more when judging small changes.

It answers ONE question: "across many maps, how often does our agent survive?" — the
number the strategy work should be chasing.

    python tools/arena.py                       # 50 games vs the RandomAgent baseline
    python tools/arena.py --games 200           # tighter estimate (less noise)
    python tools/arena.py --agent participant/src/llm_agent.py
    python tools/arena.py --max-turns 50        # mimic the shorter Discord eval

IMPORTANT — the RandomAgent baseline is too weak to be a real test. It can't kill
anything, so EVERY agent (even one that does nothing) survives ~100% against it. To
get a meaningful number, set a real opponent:

    python tools/arena.py --opponent-agent participant/src/algo_agent.py
    python tools/arena.py --opponent-agent participant/src/algo_agent.py --agent <yours>

WHAT IT MEASURES (and what it does NOT)
---------------------------------------
  YES  Survival rate across maps, with a confidence interval, plus when/how often we die.
  YES  Per-turn decide() wall time, to catch pathologically slow logic early.
  NO   It does NOT enforce the real 10s/turn deadline or the 1 CPU / 1 GiB cap — it
       calls decide() directly, in-process, on your (faster, uncapped) machine. Timing
       here is a relative smell-test, not the real budget. For the true deadline and
       resource limits, keep using `docker compose up` (the HTTP path). Two different
       checks; you need both.

Opponents are the 19 deterministic RandomAgent baselines (the stage-1 format). Each
seed is made reproducible by pinning the global RNG, so seed N -> identical game every
run. Replays are written ONLY for games you LOSE (under replays/arena/), so you can
open a failure in the viewer:
    python server/src/watch_replay.py replays/arena/seed_<n>.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import math
import os
import random
import statistics
import sys
import time
import traceback

# ── make the canonical harness + engine importable (they live in server/src) ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_SERVER_SRC = os.path.join(_REPO, "server", "src")
if _SERVER_SRC not in sys.path:
    # FIRST on the path, so `engine` and `agent_base` resolve to the canonical copy
    sys.path.insert(0, _SERVER_SRC)

from agent_base import PlayerAgent  # noqa: E402
from baseline_random import RandomAgent  # noqa: E402
from engine.actions import ActionPayload  # noqa: E402
from game_runner import GameConfig, GameRunner, PlayerRegistration  # noqa: E402
from schemas.observation import build_observation  # noqa: E402

PARTICIPANT_ID = "player-0"
# agent.py = MainAgent, the agent we actually submit (server.py loads it too);
# algo_agent.py is just the untouched starter template / fallback
DEFAULT_AGENT = os.path.join(_REPO, "participant", "src", "agent.py")
REPLAY_DIR = os.path.join(_REPO, "replays", "arena")


def load_agent_class(path: str) -> type:
    """Import the agent file by path and return its PlayerAgent subclass.

    The file's own directory is appended to sys.path (so sibling imports like the LLM
    template's `llm` resolve), but server/src stays FIRST so `engine` and `agent_base`
    resolve to the canonical engine — the agent is tested against the real rules, not
    the participant-folder mirror.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise SystemExit(f"agent file not found: {path}")
    agent_dir = os.path.dirname(path)
    if agent_dir not in sys.path:
        sys.path.append(agent_dir)
    spec = importlib.util.spec_from_file_location("agent_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, PlayerAgent)
        and obj is not PlayerAgent
    ]
    if not found:
        raise SystemExit(f"no PlayerAgent subclass found in {path}")
    return found[-1]


class ArenaRunner(GameRunner):
    """GameRunner that drives EVERY player in-process (no HTTP). Reuses the entire
    engine and turn loop; overrides only action collection. Also tracks the
    participant's per-turn decide() time, any exceptions it throws, and the turn it
    was eliminated on."""

    def __init__(self, registrations, config, actors: dict) -> None:
        super().__init__(registrations, config)
        self.actors = actors
        self.turn_times: list[float] = []
        self.participant_errors = 0
        self.first_error: str | None = None
        self.death_turn: int | None = None
        self.progress = False  # live turn counter (serial runs only)

    async def _collect_actions(self, player_urls):  # type: ignore[override]
        state = self.state
        if self.progress and state.turn_number % 10 == 0:
            print(
                f"\r       turn {state.turn_number}/{self.config.max_turns} ...",
                end="", flush=True,
            )
        # First turn we observe ourselves dead = (approximately) the turn we died.
        if self.death_turn is None and not state.players[PARTICIPANT_ID].alive:
            self.death_turn = state.turn_number

        alive = [pid for pid in player_urls if state.players[pid].alive]

        async def one(pid: str):
            obs = build_observation(
                state, pid, self.diplomacy, self.chat_log, self.config.max_turns
            )
            actor = self.actors[pid]
            if pid == PARTICIPANT_ID:
                t0 = time.perf_counter()
                try:
                    payload = await actor.decide(obs)
                except Exception:
                    # Mirror the real HttpActor: a crash becomes a no-op turn, never
                    # aborts the game. We count it because it's signal for the builder.
                    self.participant_errors += 1
                    if self.first_error is None:
                        self.first_error = traceback.format_exc()
                    payload = None
                self.turn_times.append(time.perf_counter() - t0)
            else:
                try:
                    payload = await actor.decide(obs)
                except Exception:
                    payload = None  # a flaky opponent just no-ops
            if payload is None:
                payload = ActionPayload(
                    player_id=pid, turn_number=state.turn_number, actions=[]
                )
            return pid, payload

        return dict(await asyncio.gather(*[one(pid) for pid in alive]))


def _discard_recorder(runner: GameRunner, path: str) -> None:
    """Turn off replay recording for this game and remove the (header-only) file."""
    try:
        if runner.recorder:
            runner.recorder.close()
    except Exception:
        pass
    runner.recorder = None
    try:
        os.remove(path)
    except OSError:
        pass


def run_one_game(agent_cls, seed, *, opp_cls, opponents, max_turns, map_w, map_h,
                 save_replays, progress=False):
    random.seed(seed)  # pin opponents' (and the agent's) RNG -> seed N is reproducible

    opp_ids = [f"player-{i}" for i in range(1, opponents + 1)]
    regs = [PlayerRegistration(PARTICIPANT_ID, PARTICIPANT_ID, "local://you")]
    regs += [PlayerRegistration(pid, pid, "local://opponent") for pid in opp_ids]

    actors = {PARTICIPANT_ID: agent_cls()}
    actors.update({pid: opp_cls() for pid in opp_ids})

    replay_path = os.path.join(REPLAY_DIR, f"seed_{seed}.jsonl")
    config = GameConfig(
        seed=seed,
        map_width=map_w,
        map_height=map_h,
        max_turns=max_turns,
        replay_path=replay_path,
    )

    runner = ArenaRunner(regs, config, actors)
    runner.progress = progress
    runner.initialise()
    if not save_replays:
        _discard_recorder(runner, replay_path)

    t0 = time.perf_counter()
    asyncio.run(runner.run())
    elapsed = time.perf_counter() - t0

    survived = bool(runner.state.players[PARTICIPANT_ID].alive)
    end_turn = runner.state.turn_number

    # Keep replays only for losses — the games actually worth watching.
    if save_replays and survived:
        try:
            os.remove(replay_path)
        except OSError:
            pass

    death_turn = None
    if not survived:
        death_turn = runner.death_turn if runner.death_turn is not None else end_turn

    return {
        "seed": seed,
        "survived": survived,
        "death_turn": death_turn,
        "end_turn": end_turn,
        "survivors": len(runner.state.alive_players()),
        "slowest_turn": max(runner.turn_times) if runner.turn_times else 0.0,
        "errors": runner.participant_errors,
        "first_error": runner.first_error,
        "elapsed": elapsed,
    }


def wilson_ci(k: int, n: int, z: float = 1.96):
    """95% Wilson score interval for a proportion — accurate even near 0% / 100%."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def main() -> None:
    # Windows consoles default to cp1252 and choke on non-ASCII; degrade gracefully.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="In-process survival tournament for a TIL-26 agent."
    )
    ap.add_argument("--agent", default=DEFAULT_AGENT,
                    help="path to the agent .py (default: agent.py, our real MainAgent)")
    ap.add_argument("--games", type=int, default=50,
                    help="number of seeds to play (default 50)")
    ap.add_argument("--start-seed", type=int, default=1,
                    help="first seed; seeds run start..start+games-1")
    ap.add_argument("--opponents", type=int, default=19,
                    help="number of opponents (default 19, the real format)")
    ap.add_argument("--opponent-agent", default=None,
                    help="path to the opponent agent .py (default: the RandomAgent baseline). "
                         "Point it at algo_agent.py or your own agent for a real test — "
                         "the random baseline is too weak to kill anything, so survival vs it is ~100%% for everyone.")
    ap.add_argument("--max-turns", type=int, default=300,
                    help="turn limit (default 300; try 50 for the Discord-eval length)")
    ap.add_argument("--map", default="35x30", help="map size WxH (default 35x30)")
    ap.add_argument("--no-replays", action="store_true",
                    help="don't save replays for losses (faster, less disk)")
    args = ap.parse_args()

    map_w, map_h = (int(x) for x in args.map.lower().split("x"))
    agent_cls = load_agent_class(args.agent)
    agent_name = os.path.basename(os.path.abspath(args.agent))
    if args.opponent_agent:
        opp_cls = load_agent_class(args.opponent_agent)
        opp_name = os.path.basename(os.path.abspath(args.opponent_agent))
    else:
        opp_cls = RandomAgent
        opp_name = "RandomAgent"
    save_replays = not args.no_replays

    seeds = list(range(args.start_seed, args.start_seed + args.games))
    print(
        f"arena: agent={agent_name} | {args.opponents}x {opp_name} | "
        f"{args.max_turns} turns | {map_w}x{map_h} | {args.games} games "
        f"(seeds {seeds[0]}-{seeds[-1]})"
    )
    print("-" * 60)
    print(f"{'seed':>5}  {'result':<9}{'death':>6}{'alive':>7}{'slow':>9}")

    results = []
    progress = sys.stdout.isatty()  # live turn counter only in a real terminal
    for seed in seeds:
        r = run_one_game(
            agent_cls, seed,
            opp_cls=opp_cls, opponents=args.opponents, max_turns=args.max_turns,
            map_w=map_w, map_h=map_h, save_replays=save_replays, progress=progress,
        )
        results.append(r)
        death = "-" if r["death_turn"] is None else str(r["death_turn"])
        err = "  ERR" if r["errors"] else ""
        if progress:
            print("\r" + " " * 30 + "\r", end="")  # clear the turn-counter line
        print(
            f"{r['seed']:>5}  {'SURVIVED' if r['survived'] else 'died':<9}"
            f"{death:>6}{r['survivors']:>7}{r['slowest_turn']:>8.2f}s{err}"
        )

    # ── aggregate ──────────────────────────────────────────────────────────────
    n = len(results)
    k = sum(r["survived"] for r in results)
    lo, hi = wilson_ci(k, n)
    deaths = [r["death_turn"] for r in results
              if not r["survived"] and r["death_turn"] is not None]
    survivors_when_alive = [r["survivors"] for r in results if r["survived"]]
    sole_wins = sum(1 for r in results if r["survived"] and r["survivors"] == 1)
    slowest = max((r["slowest_turn"] for r in results), default=0.0)
    total_wall = sum(r["elapsed"] for r in results)
    total_errors = sum(r["errors"] for r in results)
    first_err = next((r["first_error"] for r in results if r["first_error"]), None)

    print("-" * 60)
    print(f" SURVIVAL: {k}/{n} = {100 * k / n:.1f}%   (95% CI ~ {100 * lo:.0f}%-{100 * hi:.0f}%)")
    if deaths:
        print(
            f" losses: {len(deaths)} | death turn median {int(statistics.median(deaths))} "
            f"(earliest {min(deaths)}, latest {max(deaths)})"
        )
    if survivors_when_alive:
        print(f" when we survived: avg {statistics.mean(survivors_when_alive):.1f} players left alive")
    print(f" won outright (sole survivor): {sole_wins}/{n}")
    print(f" speed: slowest single decide() {slowest:.2f}s | total wall {total_wall:.0f}s")
    if slowest > 8.0:
        print(f"   [!] a turn hit {slowest:.1f}s - close to the real 10s limit; verify under 'docker compose up'")
    if total_errors:
        print(f" [!] agent raised {total_errors} exception(s). First traceback:")
        for ln in (first_err or "").splitlines():
            print("   " + ln)
    if save_replays and deaths:
        rel = os.path.relpath(REPLAY_DIR, _REPO).replace(os.sep, "/")
        print(f" replays for losses saved under {rel}/  (watch with server/src/watch_replay.py)")
    print("=" * 60)


if __name__ == "__main__":
    main()
