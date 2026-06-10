"""MainAgent: wires the planners together under a hard time budget.

Order matters and degrades gracefully: chat sanitization runs before anything
reads the observation; diplomacy (treaty accepts — existential, cheap) always
completes; economy (Base builds) next; per-unit military work is the part that
gets cut if the clock runs out. Unordered units simply hold, which is the
engine default anyway. Any exception → empty payload, never a crash.
"""

from __future__ import annotations

import logging
import time

from agent_base import PlayerAgent
from engine.actions import ActionPayload

import chat
import diplomacy
import economy
import military
import state

log = logging.getLogger("main_agent")

SOFT_BUDGET_SECONDS = 6.5  # leave headroom under the ~10s turn deadline


class MainAgent(PlayerAgent):
    def __init__(self) -> None:
        self.mem = state.Memory()

    async def decide(self, observation: dict) -> ActionPayload:
        deadline = time.monotonic() + SOFT_BUDGET_SECONDS
        try:
            obs = chat.sanitize(observation)
            snap = state.parse(obs)
            state.update(self.mem, snap)

            ledger = state.Ledger(snap.gold)
            claimed: set[state.Coord] = set()  # tiles reserved by planned actions

            actions = diplomacy.plan(snap, self.mem)
            actions += economy.plan(snap, self.mem, ledger, claimed, deadline)
            if time.monotonic() < deadline:
                actions += military.plan(snap, self.mem, ledger, claimed, deadline)

            return ActionPayload(
                player_id=snap.pid, turn_number=snap.turn, actions=actions
            )
        except Exception:
            log.exception("decide() failed — returning no-op turn")
            return ActionPayload(
                player_id=observation.get("player_id", ""),
                turn_number=observation.get("turn_number", 0),
                actions=[],
            )
