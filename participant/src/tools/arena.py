"""Headless multi-seed arena: the signal generator for every strategy change.

Runs N full games entirely in-process (no Docker, no HTTP): the real engine and
turn loop from server/src, our MainAgent in the player-0 slot, and 19 opponents.
Reports survival %, death turns, and decide() timing so a change can be judged
on more than one lucky seed.

    python participant/src/tools/arena.py --games 10 --turns 300
    python participant/src/tools/arena.py --games 10 --turns 50   # Discord-style

Do NOT tune to any single seed — the competition map is undisclosed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
# server/src first so the canonical engine + schemas/replay packages win;
# participant/src supplies our agent modules (state, economy, military, ...).
sys.path.insert(0, str(_ROOT / "participant" / "src"))
sys.path.insert(0, str(_ROOT / "server" / "src"))

from baseline_random import RandomAgent  # noqa: E402
from eval_harness import HarnessRunner  # noqa: E402
from game_runner import GameConfig, PlayerRegistration  # noqa: E402

from agent import MainAgent  # noqa: E402

ME = "player-0"
NUM_OPPONENTS = 19


class TimedActor:
    """Wraps an in-process agent, recording wall time and swallowing errors the
    same way the HTTP boundary would (error → no-op turn)."""

    def __init__(self, agent) -> None:
        self.agent = agent
        self.times: list[float] = []
        self.errors = 0

    async def decide(self, observation: dict):
        t0 = time.perf_counter()
        try:
            return await self.agent.decide(observation)
        except Exception:  # noqa: BLE001
            self.errors += 1
            return None
        finally:
            self.times.append(time.perf_counter() - t0)


def run_game(seed: int, turns: int, replay_dir: Path) -> dict:
    regs = [PlayerRegistration(ME, ME, "local://me")]
    actors: dict = {ME: TimedActor(MainAgent())}
    for i in range(1, NUM_OPPONENTS + 1):
        pid = f"player-{i}"
        regs.append(PlayerRegistration(pid, pid, "local://opponent"))
        actors[pid] = RandomAgent()

    config = GameConfig(
        seed=seed,
        max_turns=turns,
        replay_path=str(replay_dir / f"seed{seed}_t{turns}.jsonl"),
    )
    runner = HarnessRunner(regs, config, actors)
    runner.initialise()
    asyncio.run(runner.run())

    me = runner.state.players[ME]
    death_turn = None
    for m in runner.chat_log.messages:
        if m.sender_id == "__system__" and f"{ME} has been eliminated" in m.text:
            death_turn = m.turn
            break

    actor = actors[ME]
    return {
        "seed": seed,
        "alive": bool(me.alive),
        "death_turn": death_turn,
        "end_turn": runner.state.turn_number,
        "bases": runner.state.count_bases(ME),
        "gold": me.resources.to_dict().get("gold", 0),
        "decide_max_s": max(actor.times, default=0.0),
        "decide_p95_s": (
            statistics.quantiles(actor.times, n=20)[-1] if len(actor.times) >= 20 else 0.0
        ),
        "decide_errors": actor.errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=5)
    parser.add_argument("--turns", type=int, default=300)
    parser.add_argument("--seed", type=int, default=1000, help="first seed; games use seed..seed+games-1")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)  # silence per-turn engine logs
    replay_dir = _ROOT / "replays" / "arena"
    os.makedirs(replay_dir, exist_ok=True)

    results = []
    t_start = time.perf_counter()
    for seed in range(args.seed, args.seed + args.games):
        r = run_game(seed, args.turns, replay_dir)
        results.append(r)
        status = "SURVIVED" if r["alive"] else f"DIED turn {r['death_turn']}"
        print(
            f"seed {r['seed']:>5}: {status:<16} bases={r['bases']} gold={r['gold']:>5} "
            f"decide max={r['decide_max_s']:.2f}s p95={r['decide_p95_s']:.2f}s "
            f"errors={r['decide_errors']}"
        )

    n = len(results)
    survived = sum(1 for r in results if r["alive"])
    deaths = [r["death_turn"] for r in results if r["death_turn"] is not None]
    worst_decide = max(r["decide_max_s"] for r in results)
    print("\n" + "=" * 60)
    print(f"  games: {n}  turns: {args.turns}  wall: {time.perf_counter() - t_start:.0f}s")
    print(f"  SURVIVAL: {survived}/{n} ({100 * survived / n:.0f}%)")
    if deaths:
        print(f"  death turns: {sorted(deaths)} (median {statistics.median(deaths)})")
    print(f"  worst decide(): {worst_decide:.2f}s  (budget ~10s)")
    print(f"  total decide errors: {sum(r['decide_errors'] for r in results)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
