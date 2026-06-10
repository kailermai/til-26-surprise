"""Parallel front-end for arena.py — same games, same stats, all CPU cores.

Each worker process loads the agents itself and plays whole games via
arena.run_one_game; the parent only farms out seeds and aggregates. Seeds are
pinned per game exactly as in arena.py, so results are reproducible and match
a serial run — per-seed lines just print in completion order, not seed order.

    python tools/arena_parallel.py --games 50 --opponent-agent participant/src/agent.py

Flags mirror arena.py, plus --workers (default: all-but-two logical CPUs so the
laptop stays usable).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import arena  # noqa: E402  (also puts server/src first on sys.path)

_CLS_CACHE: dict[str, type] = {}


def _cls(path: str | None) -> type:
    if path is None:
        return arena.RandomAgent
    cls = _CLS_CACHE.get(path)
    if cls is None:
        cls = arena.load_agent_class(path)
        _CLS_CACHE[path] = cls
    return cls


def _play(agent_path, opp_path, seed, opponents, max_turns, map_w, map_h, save_replays):
    """Runs in a worker process. Agent classes are loaded once per process."""
    return arena.run_one_game(
        _cls(agent_path), seed,
        opp_cls=_cls(opp_path), opponents=opponents, max_turns=max_turns,
        map_w=map_w, map_h=map_h, save_replays=save_replays,
    )


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    default_workers = max(1, (os.cpu_count() or 4) - 2)
    ap = argparse.ArgumentParser(
        description="Parallel in-process survival tournament (arena.py on all cores)."
    )
    ap.add_argument("--agent", default=arena.DEFAULT_AGENT,
                    help="path to the agent .py (default: agent.py, our real MainAgent)")
    ap.add_argument("--games", type=int, default=50,
                    help="number of seeds to play (default 50)")
    ap.add_argument("--start-seed", type=int, default=1,
                    help="first seed; seeds run start..start+games-1")
    ap.add_argument("--opponents", type=int, default=19,
                    help="number of opponents (default 19, the real format)")
    ap.add_argument("--opponent-agent", default=None,
                    help="path to the opponent agent .py (default: the RandomAgent baseline, "
                         "which is too weak to kill anything — pass our own agent for real signal)")
    ap.add_argument("--max-turns", type=int, default=300,
                    help="turn limit (default 300; try 50 for the Discord-eval length)")
    ap.add_argument("--map", default="35x30", help="map size WxH (default 35x30)")
    ap.add_argument("--no-replays", action="store_true",
                    help="don't save replays for losses (faster, less disk)")
    ap.add_argument("--workers", type=int, default=default_workers,
                    help=f"parallel worker processes (default {default_workers})")
    args = ap.parse_args()

    map_w, map_h = (int(x) for x in args.map.lower().split("x"))
    agent_path = os.path.abspath(args.agent)
    opp_path = os.path.abspath(args.opponent_agent) if args.opponent_agent else None
    arena.load_agent_class(agent_path)  # fail fast in the parent on a bad path
    if opp_path:
        arena.load_agent_class(opp_path)
    agent_name = os.path.basename(agent_path)
    opp_name = os.path.basename(opp_path) if opp_path else "RandomAgent"
    save_replays = not args.no_replays
    if save_replays:
        os.makedirs(arena.REPLAY_DIR, exist_ok=True)

    seeds = list(range(args.start_seed, args.start_seed + args.games))
    print(
        f"arena[parallel x{args.workers}]: agent={agent_name} | "
        f"{args.opponents}x {opp_name} | {args.max_turns} turns | {map_w}x{map_h} | "
        f"{args.games} games (seeds {seeds[0]}-{seeds[-1]})"
    )
    print("-" * 60)
    print(f"{'seed':>5}  {'result':<9}{'death':>6}{'alive':>7}{'slow':>9}")

    t0 = time.perf_counter()
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(
                _play, agent_path, opp_path, seed, args.opponents,
                args.max_turns, map_w, map_h, save_replays,
            )
            for seed in seeds
        ]
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            death = "-" if r["death_turn"] is None else str(r["death_turn"])
            err = "  ERR" if r["errors"] else ""
            print(
                f"{r['seed']:>5}  {'SURVIVED' if r['survived'] else 'died':<9}"
                f"{death:>6}{r['survivors']:>7}{r['slowest_turn']:>8.2f}s{err}"
            )
    wall = time.perf_counter() - t0

    # ── aggregate (same stats as arena.py) ────────────────────────────────────
    n = len(results)
    k = sum(r["survived"] for r in results)
    lo, hi = arena.wilson_ci(k, n)
    deaths = [r["death_turn"] for r in results
              if not r["survived"] and r["death_turn"] is not None]
    survivors_when_alive = [r["survivors"] for r in results if r["survived"]]
    sole_wins = sum(1 for r in results if r["survived"] and r["survivors"] == 1)
    slowest = max((r["slowest_turn"] for r in results), default=0.0)
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
    print(f" speed: slowest single decide() {slowest:.2f}s | wall {wall:.0f}s "
          f"({n / wall * 60:.1f} games/min)")
    if slowest > 8.0:
        print(f"   [!] a turn hit {slowest:.1f}s - close to the real 10s limit; "
              f"workers contend for CPU, so verify under 'docker compose up'")
    if total_errors:
        print(f" [!] agent raised {total_errors} exception(s). First traceback:")
        for ln in (first_err or "").splitlines():
            print("   " + ln)
    if save_replays and deaths:
        rel = os.path.relpath(arena.REPLAY_DIR, arena._REPO).replace(os.sep, "/")
        print(f" replays for losses saved under {rel}/  (watch with server/src/watch_replay.py)")
    print("=" * 60)


if __name__ == "__main__":
    main()
