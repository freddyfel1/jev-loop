"""The hard risk caps, in one place. Never overridable by a strategy.

This is not the file you edit to change how the loop trades: that file is
strategy.py, which owns the tunable thresholds compose_action() uses and
a hook that can override or veto its output. This file owns the ceiling
that hook can never raise: nine limits, checked in risk.py before every
order, plus the operational numbers for ladder.py, pricing.py, and
execution/alpaca.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Limits:
    # --- risk.py hard vetoes (checked before every order, never delegated) ---
    # Expressed in dollars, not base units, so the same defaults make sense
    # whether the asset is an $85,000 coin or a $30 stock.
    max_position_usd: float = 50.0  # max absolute position value
    max_daily_loss_usd: float = 25.0  # kill switch on realised + unrealised loss today
    max_drawdown_pct: float = 0.05  # kill switch: 5% below the session high-water mark
    max_order_notional_usd: float = 25.0  # dollar value of a single order
    max_inventory_age_s: float = (
        900.0  # 15 minutes holding non-flat inventory before veto
    )
    max_stale_data_age_s: float = 5.0  # market data older than this is refused
    max_api_errors: int = 5  # consecutive broker/API errors before kill
    max_decision_latency_ms: float = (
        2000.0  # Jev slower than this on a block = late, hold
    )
    max_leverage: float = 1.0  # spot/cash only, no leverage, ever

    # The seven policy thresholds compose_action() used to read from here
    # (toxic flow, liquidity stress, quote environment, inventory pressure,
    # direction confidence) now live in strategy.py as StrategyThresholds.
    # That is the file you edit to change how the loop decides. This file
    # stays the hard ceiling a strategy can never raise.

    # --- ladder.py ---
    low_confidence_threshold: float = (
        0.50  # below this on a taken decision -> REDUCE rung
    )
    reduce_size_factor: float = 0.5  # order size multiplier while on the REDUCE rung

    # --- pricing.py (Avellaneda-Stoikov) ---
    as_gamma: float = 0.10  # risk aversion
    as_kappa: float = 1.5  # order book liquidity / arrival-rate parameter
    as_horizon_s: float = 60.0  # T - t, the inventory-clearing horizon in seconds

    # --- execution/alpaca.py ---
    tick_seconds: float = 2.0  # "block" = one tick of this loop. See README for why 2s.
    rest_ticks: int = 3  # rest a resting quote this many ticks before cancel-replace
    quote_notional_usd: float = 20.0  # dollar target for each side of a quote
    directional_notional_usd: float = (
        20.0  # dollar target for the directional-leg order
    )
    buy_ceiling_fraction: float = (
        0.9  # new buys stop at 90% of max_position_usd, so order rounding or a
        # small price rise can't push a full position over the hard cap (a KILL)
    )
    max_alpaca_calls_per_minute: int = (
        180  # ~6 calls a tick (book, trades, position, open orders, quotes) at 30 ticks/min;
        # under Alpaca's free tier, which allows 200/min on trading and on data separately
    )
