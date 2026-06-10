"""Incoming-chat sanitization — the flood/injection defense.

Chat in the observation is uncapped (full cumulative history, unbounded message
size), so an opponent can flood it to blow our 10s budget or inject text aimed
at an LLM. `sanitize()` runs FIRST in decide(), before anything else touches the
observation. The deterministic planners never read chat *text* at all (only the
structured treaty fields), so injection is inert by construction — this cap is
about time and memory.
"""

from __future__ import annotations

MAX_MESSAGES = 50  # keep only the newest N per channel
MAX_TEXT_CHARS = 500  # truncate each message body


def delta(obs: dict, after_turn: int) -> list[dict]:
    """Messages newer than `after_turn` from both channels, excluding our own,
    flattened to {turn, channel, sender, text} and sorted by turn. Pure read —
    call it only on an obs that already went through sanitize()."""
    pid = obs.get("player_id")
    out: list[dict] = []
    for key, channel in (("global_chat", "global"), ("private_chat", "dm")):
        for m in obs.get(key, []):
            t = m.get("turn")
            if not isinstance(t, int) or t <= after_turn or m.get("sender_id") == pid:
                continue
            text = m.get("text")
            out.append(
                {
                    "turn": t,
                    "channel": channel,
                    "sender": str(m.get("sender_id", "?")),
                    "text": text if isinstance(text, str) else "",
                }
            )
    out.sort(key=lambda m: m["turn"])
    return out


def sanitize(obs: dict) -> dict:
    for key in ("global_chat", "private_chat"):
        msgs = obs.get(key)
        if not isinstance(msgs, list):
            obs[key] = []
            continue
        if len(msgs) > MAX_MESSAGES:
            msgs = msgs[-MAX_MESSAGES:]
        clean = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            text = m.get("text")
            if isinstance(text, str) and len(text) > MAX_TEXT_CHARS:
                m = {**m, "text": text[:MAX_TEXT_CHARS]}
            clean.append(m)
        obs[key] = clean
    return obs
