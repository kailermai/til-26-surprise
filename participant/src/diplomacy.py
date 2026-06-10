"""Treaty policy: accept every peace, propose to everyone we meet.

While a treaty is ACTIVE the engine silently fails attacks against us, so peace
is pure upside. The break countdown does NOT protect us (the engine only blocks
attacks for ACTIVE treaties), so a breaking partner is treated as hostile the
turn the break appears — that reaction lives in state.Memory.update / military.
All treaties void at TREATY_CUTOFF_TURN; past it this planner emits nothing.
"""

from __future__ import annotations

from engine.actions import ProposeTreatyAction, RespondTreatyAction, SendChatAction
from engine.constants import TREATY_CUTOFF_TURN

from state import Memory, Snapshot

REPROPOSE_EVERY = 8  # turns between re-proposals to a silent player
MAX_GREETINGS_PER_TURN = 2

GREETING = (
    "Proposing peace. We never attack treaty partners and we never break first."
)


def plan(snap: Snapshot, mem: Memory) -> list:
    if snap.turn >= TREATY_CUTOFF_TURN:
        return []

    actions: list = []

    # accept every incoming peace proposal
    incoming: set[str] = set()
    for p in snap.proposals:
        incoming.add(p["proposer_id"])
        actions.append(
            RespondTreatyAction(
                proposing_player_id=p["proposer_id"],
                treaty_type=p.get("treaty_type", "peace"),
                accept=True,
            )
        )

    # propose to every known player we don't already have a treaty with
    greetings = 0
    for pid in snap.known_players:
        if pid == snap.pid or pid in snap.treaties or pid in incoming:
            continue
        if snap.turn - mem.last_proposed.get(pid, -REPROPOSE_EVERY) < REPROPOSE_EVERY:
            continue
        actions.append(ProposeTreatyAction(target_player_id=pid))
        mem.last_proposed[pid] = snap.turn
        if pid not in mem.greeted and greetings < MAX_GREETINGS_PER_TURN:
            actions.append(SendChatAction(text=GREETING, recipient_id=pid))
            mem.greeted.add(pid)
            greetings += 1

    return actions
