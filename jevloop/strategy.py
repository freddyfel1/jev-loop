"""strategy.py -- the file you edit to change how this loop trades.

This is the harness the video talks about, not the edge. Everything it
shipped with is a generic strategy pulled out of thin air, wired in only
so the demo has something to trade. Real strategies are hard to build
properly; this file is where yours goes.

Two things live here:

1. `StrategyThresholds`, one number per action in `compose_action()`
   (policy.py): when to pull quotes, when to widen, when the quote
   environment is good enough to quote both sides or just quote wide, how
   much inventory pressure skews sizing, and how confident Jev has to be
   about direction before a directional leg is taken. Change a number,
   restart the loop, see different behaviour on the next tick.

2. `apply_strategy()`, a hook called once per tick with the action
   `compose_action()` already produced from the thresholds above. Return
   it unchanged (the default) and nothing changes from what shipped in
   the video. Return a different action to override it, or an action
   with `kind=STAND_DOWN` to veto the tick outright. This is the one
   function a real strategy plugs into.

Shipped default: every threshold below matches what the video ran, and
`apply_strategy()` is a no-op. Nothing changes for someone who never
opens this file.

The hard risk caps (max position, max daily loss, max drawdown, and so
on) do NOT live here: they live in `jevloop/limits.py`, checked in
`risk.py` before every order, and this file cannot raise them. A
strategy can make the loop more conservative than the risk engine
allows; it can never make it less conservative than the risk engine
allows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StrategyThresholds:
    """One threshold per action in compose_action(). These are exactly the
    numbers the video shipped with."""

    toxic_flow_pull_threshold: float = 0.6  # ans["toxic_flow"] > this -> PULL_QUOTES
    liquidity_stressed_widen_threshold: float = (
        0.7  # ans["liquidity_stressed"] > this -> WIDEN
    )
    quote_env_full_score: float = 2.0  # env >= this and confident -> quote both sides
    quote_env_full_confidence: float = 0.80
    quote_env_wide_score: float = 1.0  # env >= this (but below full) -> quote wide
    inventory_pressure_max_score: float = 3.0  # denominator for the skew calculation

    # the directional leg, bolted on so the demo shows fills, not just quotes.
    # OFF (2026-09-24, briefly back on 09:51-10:00 that day, no legs taken).
    # Calibrations of Jev's direction call up to 4,170 real-Jev decisions
    # (30-tick horizon) found no signal: Brier 0.33, skill -0.32; price rose
    # ~50% of the time whatever Jev said, and its ~7% "up" calls were
    # followed by a rise 58% of the time. Set True to take legs again, gated
    # by the threshold below, once `jev-loop calibrate` shows skill > 0.
    directional_legs_enabled: bool = False
    direction_confidence_threshold: float = (
        0.75  # direction.confidence above this -> take the leg
    )
    # Raised from the video's 0.55 on 2026-09-23. Confidence c on the
    # three-way up/down/neutral call means P(chosen side) ~ (2c + 1) / 3, so
    # 0.55 traded at ~70% and 0.75 needs ~83%. The first calibration (219
    # real-Jev decisions, 30-tick horizon) found calls at 70-80% were right
    # 38% of the time and 80%+ only 44%: overconfident. At 0.75 the leg
    # fired on 1 of 386 logged ticks, so the loop mostly just quotes until
    # `jev-loop calibrate` shows Jev's high-confidence calls hold up.


THRESHOLDS = StrategyThresholds()


def apply_strategy(action, answers: dict, snapshot: dict, limits) -> object:
    """The strategy hook. Called once per tick, after compose_action() has
    already turned Jev's answers into an action using THRESHOLDS above.

    Default: return the action unchanged. That is the whole strategy the
    video ran, a generic one, pulled out of thin air, applied only so the
    demo shows real fills.

    To plug in your own strategy, edit this function. You can:
      - inspect `answers` (the seven Jev judgments this tick) or
        `snapshot` (the deterministic state) and return a different
        Action than the one compose_action() chose
      - veto the tick outright by returning
        `Action(STAND_DOWN, reason="my strategy said no")`
      - leave `action` alone most of the time and only step in on
        specific conditions

    `risk.py` still runs after this and still has the final veto, so
    nothing returned here can bypass a hard limit in limits.py, only add
    more caution on top of it.
    """
    return action
