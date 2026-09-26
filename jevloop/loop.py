"""The nine-stage block loop.

    1. block event            (tick clock, "block" = one tick, default 2s)
    2. read the book          (Alpaca paper market data: L2 depth on crypto,
                               best bid/ask on equities, and recent trades)
    3. state snapshot         (deterministic, state.py)
    4. battery                (seven Jev judgments, one call, battery.py)
    5. policy engine          (compose_action, policy.py: code, not Jev)
    6. pricing                (Avellaneda-Stoikov, pricing.py: code)
    7. risk veto              (hard limits, risk.py: code, absolute veto)
    8. execute                (cancel-replace post-only quotes plus directional leg)
    9. log, fills, inventory  (JSONL and latest.json for the dashboard)

Paper by default. `--mock` forces the mock decision client even if a real
key is present, useful for a clean demo run that can never fail on network
or billing grounds. `--dry-execution` reads real market data and fires a
real Jev battery but never submits an order to Alpaca; every fill line
reads "dry" instead. Useful for testing the full pipeline without touching
the paper account. `--ticks 0` or `--forever` runs continuously until
stopped (Ctrl+C, or a SIGTERM if it is running in the background), and
cancels any resting orders before it exits rather than leaving them
behind. `--live` is a separate, deliberately awkward opt-in documented in
execution/alpaca.py and SKILL.md; paper is what every default here
resolves to unless a caller goes out of its way to ask for live.

Any asset `jevloop/assets.py` resolves: a crypto pair runs 24/7; a US
equity ticker only trades while the market is open, and the loop holds
(never orders) while it is closed, per `execution/alpaca.py`'s market-hours
guard.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from .assets import AssetSpec, UnknownSymbolError, resolve_symbol, size_order
from .battery import run_battery
from .client import (
    DecisionClientError,
    GatewayVerificationRequired,
    resolve_decision_client,
)
from .execution.alpaca import (
    LIVE_CONFIRMATION_PHRASE,
    AlpacaAPIError,
    AlpacaConfigError,
    LiveTradingRefused,
    MarketClosedError,
    client_from_env,
)
from .ladder import Rung, select_rung
from .limits import Limits
from .policy import (
    KILL,
    PULL_QUOTES,
    QUOTE_BOTH_SIDES,
    QUOTE_WIDE,
    STAND_DOWN,
    WIDEN,
    compose_action,
    fallback_action,
)
from .pricing import quote_prices
from .state import InventoryState, build_snapshot, record_fill_slippage, update_vwap

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path(__file__).resolve().parent.parent / "data")))
LOG_FILE = LOG_DIR / "log.jsonl"
LATEST_FILE = LOG_DIR / "latest.json"
# Creating this file asks a running loop to stop cleanly (cancel resting
# orders, then exit), the same as Ctrl+C: what stop-bot.ps1 does.
STOP_FILE = LOG_DIR / "stop.request"
LATEST_WINDOW = 120


def _fmt_money(x: float) -> str:
    return f"{x:,.1f}"


class _StopRequested(Exception):
    """Raised by the SIGTERM handler so a run stopped from the background
    (`kill <pid>`) shuts down exactly as cleanly as a foreground Ctrl+C
    (KeyboardInterrupt) does: cancel resting orders, then exit."""


def _handle_sigterm(signum, frame) -> None:
    raise _StopRequested()


def _check_stop_file() -> None:
    """Raise _StopRequested if a stop was requested through STOP_FILE,
    consuming the request so the next run doesn't stop at once."""
    if STOP_FILE.exists():
        try:
            STOP_FILE.unlink()
        except OSError:
            pass
        raise _StopRequested()


def run(
    symbol: str,
    ticks: int | None,
    mock: bool,
    limits: Limits,
    dry_execution: bool = False,
    live: bool = False,
    confirmation: str | None = None,
) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        spec = resolve_symbol(symbol)
    except UnknownSymbolError as exc:
        print(f"cannot start: {exc}")
        return 1

    try:
        alpaca = client_from_env(
            symbol=spec.symbol,
            live=live,
            confirmation=confirmation,
            calls_per_minute=limits.max_alpaca_calls_per_minute,
        )
    except AlpacaConfigError as exc:
        print(f"cannot start: {exc}")
        return 1
    except LiveTradingRefused as exc:
        print(f"cannot start: {exc}")
        return 1

    client = resolve_decision_client(mock=mock)
    session_txt = "24/7" if spec.is_24_7 else "market hours only"
    print(
        f"asset: {spec.symbol} ({spec.asset_class}, {session_txt}, "
        f"min order ${spec.min_notional_usd:.2f}, depth: {'yes' if spec.has_depth else 'best bid/ask only'})"
    )
    print(
        f"tick cadence: one block every {limits.tick_seconds:.1f}s "
        f"(Alpaca calls capped at {limits.max_alpaca_calls_per_minute}/min)"
    )
    print(
        "your strategy lives in strategy.py (edit thresholds, or the apply_strategy hook)"
    )
    if ticks is None:
        print(
            "running continuously until stopped (Ctrl+C, or `kill <pid>` if this is "
            "in the background). Resting orders are cancelled on shutdown."
        )
    if dry_execution:
        print(
            "dry execution: real market data and a real Jev battery, no orders sent to Alpaca."
        )

    inv = InventoryState(equity_usd=0.0, high_water_mark_usd=0.0)
    # Position at the end of the previous tick, to spot resting quotes
    # that filled in between. None until the first tick has run.
    prev_position: float | None = None
    api_error_streak = 0
    jev_down = False
    rest_counter = 0
    resting_quotes: dict | None = None
    recent_ticks: list[dict] = []
    block = 0
    started_at = time.time()
    run_id = f"{started_at:.0f}"  # separates runs in log.jsonl for calibrate.py
    # Fills since this run started, for the dashboard: resting quotes fill
    # between ticks, so they are fetched from Alpaca rather than seen here.
    fill_stats = _new_fill_stats()
    seen_fill_ids: set = set()
    fills_cursor = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at))
    start_equity: float | None = None

    n = 0
    previous_sigterm_handler = None
    try:
        previous_sigterm_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, AttributeError, OSError):
        pass  # not the main thread, or a platform without SIGTERM

    # A stop request left over from before this run started is stale.
    try:
        STOP_FILE.unlink()
    except OSError:
        pass

    try:
        while ticks is None or n < ticks:
            _check_stop_file()
            tick_start = time.monotonic()
            block += 1
            now = time.time()

            # Market-hours guard for equities: never even ask Jev about a
            # frozen market, and never place an order while it is closed.
            if not spec.is_24_7:
                try:
                    market_open = alpaca.is_market_open()
                except AlpacaAPIError as exc:
                    api_error_streak += 1
                    print(f"tick {block} | alpaca error checking market hours: {exc}")
                    _sleep_remaining(tick_start, limits.tick_seconds)
                    n += 1
                    continue
                if not market_open:
                    record = _closed_market_record(block, now, spec.symbol)
                    record["run_id"] = run_id
                    _append_log(record)
                    recent_ticks.append(record)
                    if len(recent_ticks) > LATEST_WINDOW:
                        recent_ticks = recent_ticks[-LATEST_WINDOW:]
                    _write_latest(
                        spec.symbol,
                        block,
                        recent_ticks,
                        {"route": None, "model": None},
                        started_at,
                        api_error_streak,
                    )
                    print(
                        f"tick {block} | {spec.symbol} market is closed, prices are stale | HOLD"
                    )
                    n += 1
                    _sleep_remaining(tick_start, limits.tick_seconds)
                    continue

            # 2. read the book
            try:
                bids, asks = _read_top_of_book(alpaca, spec)
                trade = alpaca.get_latest_trade()
                recent = alpaca.get_recent_trades(limit=100)
                api_error_streak = 0
            except AlpacaAPIError as exc:
                api_error_streak += 1
                print(f"tick {block} | alpaca error: {exc}")
                _sleep_remaining(tick_start, limits.tick_seconds)
                n += 1
                continue

            # 2b. reconcile inventory with the broker. Resting quotes fill
            # between ticks and a restart starts from nothing, so the only
            # honest inventory is the one Alpaca reports, read every tick.
            try:
                pos_qty, pos_avg_px = alpaca.get_position()
                open_buy_usd = alpaca.get_open_buy_notional_usd()
            except AlpacaAPIError as exc:
                api_error_streak += 1
                print(f"tick {block} | alpaca error reading position: {exc}")
                _sleep_remaining(tick_start, limits.tick_seconds)
                n += 1
                continue
            quote_fill, quote_fill_qty = _position_change(prev_position, pos_qty)
            _sync_inventory(inv, pos_qty, pos_avg_px, now)

            mid = (
                (bids[0][0] + asks[0][0]) / 2
                if bids and asks and bids[0][0] and asks[0][0]
                else float(trade.get("p", 0.0))
            )
            microprice = mid  # depth-weighted microprice; mid is a fair stand-in when depth is thin
            spread_bps = (
                (asks[0][0] - bids[0][0]) / mid * 10_000
                if (mid and bids and asks)
                else 0.0
            )

            data_ts = now
            trade_prices = [(now - i * 0.3, mid) for i in range(len(recent))] or [
                (now, mid)
            ]
            trade_sides = [
                (now - i * 0.3, "buy" if t.get("tks") == "B" else "sell")
                for i, t in enumerate(recent)
            ] or [(now, "buy")]
            trades_for_vwap = [
                (now - i * 0.3, float(t.get("p", mid)), float(t.get("s", 0.0)))
                for i, t in enumerate(recent)
            ]
            update_vwap(inv, trades_for_vwap)

            # 3. state snapshot
            snapshot = build_snapshot(
                as_of=now,
                mid=mid,
                microprice=microprice,
                spread_bps=spread_bps,
                bid_depth=bids,
                ask_depth=asks,
                trade_prices=trade_prices,
                trade_sides=trade_sides,
                inv=inv,
                data_timestamp=data_ts,
                has_depth=spec.has_depth,
            )

            # 4. battery (respecting the block deadline)
            elapsed = time.monotonic() - tick_start
            budget = max(0.05, limits.tick_seconds - elapsed - 0.15)
            decision_late = False
            answers = None
            meta = {"model": None, "latency_ms": None, "route": None}
            try:
                answers, meta = run_battery(client, snapshot, timeout=budget)
                jev_down = False
            except GatewayVerificationRequired as exc:
                print(
                    f"tick {block} | gateway needs a card on file: {exc}\n"
                    f"           add one at https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%3Fmodal%3Dadd-credit-card, "
                    f"falling back to the mock decision client for the rest of this run."
                )
                client = resolve_decision_client(mock=True)
                jev_down = True
            except DecisionClientError as exc:
                msg = str(exc)
                if "deadline" in msg:
                    decision_late = True
                else:
                    jev_down = True
                    print(f"tick {block} | decision client error: {exc}")

            # 5. policy engine (code) + 6. pricing (code)
            if decision_late:
                action = None
            elif jev_down or answers is None:
                action = fallback_action(snapshot, limits)
            else:
                action = compose_action(answers, snapshot, limits)

            sigma = snapshot["realised_vol_short"] or 0.001
            bid_px, ask_px = quote_prices(
                mid=mid,
                inventory=snapshot["inventory"],
                sigma=sigma,
                gamma=limits.as_gamma,
                kappa=limits.as_kappa,
                time_left_s=limits.as_horizon_s,
            )

            # ladder
            decision_conf = None
            if action is not None and answers is not None:
                decision_conf = answers.get("quote_environment", {}).get("confidence")
            execution_health_score = (
                answers["execution_health"]["score"] if answers else None
            )
            risk_kill_pre = snapshot["drawdown_pct"] > limits.max_drawdown_pct
            rung = select_rung(
                risk_kill=risk_kill_pre,
                decision_late=decision_late,
                jev_down=jev_down and not decision_late,
                decision_confidence=decision_conf,
                low_confidence_threshold=limits.low_confidence_threshold,
                execution_health_score=execution_health_score,
            )

            size_factor = limits.reduce_size_factor if rung == Rung.REDUCE else 1.0
            quote_notional = limits.quote_notional_usd * size_factor
            directional_notional = limits.directional_notional_usd * size_factor

            fill_txt = "-"
            fill_qty = None
            fill_price = None
            killed = False
            if rung == Rung.HOLD_LATE or action is None:
                line_action = "HOLD (late)"
            elif rung == Rung.KILL or (action and action.kind == KILL):
                line_action = "KILL (flatten)"
                killed = True
            else:
                # 7. risk veto happens inside execute_action via risk.check
                (
                    line_action,
                    fill_txt,
                    fill_qty,
                    fill_price,
                    resting_quotes,
                    rest_counter,
                    killed,
                ) = _execute_action(
                    alpaca=alpaca,
                    spec=spec,
                    action=action,
                    bid_px=bid_px,
                    ask_px=ask_px,
                    mid=mid,
                    quote_notional=quote_notional,
                    directional_notional=directional_notional,
                    snapshot=snapshot,
                    limits=limits,
                    inv=inv,
                    api_error_streak=api_error_streak,
                    decision_latency_ms=meta.get("latency_ms"),
                    resting_quotes=resting_quotes,
                    rest_counter=rest_counter,
                    now=now,
                    open_buy_usd=open_buy_usd,
                    dry=dry_execution,
                )

            kill_txt = None
            if killed:
                # A KILL is flatten-and-stop, not a line in the log: cancel
                # every resting order, close the position at market, stop.
                kill_txt = _kill_switch(alpaca, dry=dry_execution)
                resting_quotes = None

            # 9. log
            record = {
                "tick": block,
                "ts": now,
                "symbol": spec.symbol,
                "mid": mid,
                "vwap": snapshot["vwap"],
                "spread_bps": round(spread_bps, 2),
                "has_depth": spec.has_depth,
                "regime": answers["regime"]["choice"] if answers else None,
                "regime_conf": answers["regime"]["confidence"] if answers else None,
                "run_id": run_id,
                "direction": answers["direction"]["choice"] if answers else None,
                # Jev's own P(up), what calibrate.py scores. Not a proxy.
                "p_up": (
                    answers["direction"].get("probabilities", {}).get("up")
                    if answers
                    else None
                ),
                "toxic_flow": answers["toxic_flow"]["noul"] if answers else None,
                "liquidity_stressed": (
                    answers["liquidity_stressed"]["noul"] if answers else None
                ),
                "quote_environment": (
                    answers["quote_environment"]["score"] if answers else None
                ),
                "quote_environment_conf": (
                    answers["quote_environment"]["confidence"] if answers else None
                ),
                "inventory_pressure": (
                    answers["inventory_pressure"]["score"] if answers else None
                ),
                "execution_health": (
                    answers["execution_health"]["score"] if answers else None
                ),
                "action": action.kind if action else "HOLD_LATE",
                "action_reason": action.reason if action else "block deadline exceeded",
                "direction_leg": action.direction_leg if action else None,
                "skew": action.skew if action else 0.0,
                "rung": rung.value,
                "late": rung == Rung.HOLD_LATE,
                "latency_ms": meta.get("latency_ms"),
                "model": meta.get("model"),
                "route": meta.get("route"),
                "inventory": inv.inventory,
                "unrealised_pnl_usd": snapshot["unrealised_pnl_usd"],
                "drawdown_pct": snapshot["drawdown_pct"],
                "fill": fill_txt,
                "fill_qty": fill_qty,
                "fill_price": fill_price,
                # A resting quote that filled since the last tick, seen as a
                # change in the broker position: "buy", "sell" or None.
                "quote_fill": quote_fill,
                "quote_fill_qty": quote_fill_qty,
                "open_buy_usd": round(open_buy_usd, 2),
                "killed": killed,
                "kill_result": kill_txt,
            }
            _append_log(record)
            # After this tick's own market leg, so a leg is not counted
            # again as a quote fill on the next tick.
            prev_position = inv.inventory
            recent_ticks.append(record)
            if len(recent_ticks) > LATEST_WINDOW:
                recent_ticks = recent_ticks[-LATEST_WINDOW:]
            if not dry_execution and block % FILL_POLL_TICKS == 1:
                try:
                    fills_cursor = (
                        _tally_fills(
                            alpaca.get_fills_since(fills_cursor),
                            fill_stats,
                            seen_fill_ids,
                            spec.symbol,
                        )
                        or fills_cursor
                    )
                    equity = float(alpaca.get_account().get("equity") or 0.0)
                    if start_equity is None:
                        start_equity = equity
                    fill_stats["equity_change_usd"] = round(equity - start_equity, 2)
                except (AlpacaAPIError, ValueError):
                    pass  # dashboard-only: try again next time
            fill_stats["pnl_usd"] = _run_pnl(fill_stats, mid)
            _write_latest(
                spec.symbol,
                block,
                recent_ticks,
                meta,
                started_at,
                api_error_streak,
                fills=None if dry_execution else fill_stats,
            )

            regime_txt = (
                f"{answers['regime']['choice']} {answers['regime']['confidence']*100:.0f}%"
                if answers
                else "n/a"
            )
            tox_txt = f"{answers['toxic_flow']['noul']:.2f}" if answers else "n/a"
            env_txt = (
                f"{answers['quote_environment']['score']:.1f}" if answers else "n/a"
            )
            xh_txt = f"{answers['execution_health']['score']:.1f}" if answers else "n/a"
            ms_txt = (
                f"{meta.get('latency_ms'):.0f} ms" if meta.get("latency_ms") else "late"
            )
            print(
                f"tick {block} | mid {_fmt_money(mid)} | regime {regime_txt} | "
                f"tox {tox_txt} | env {env_txt} | xh {xh_txt} | {ms_txt} | {line_action} | {fill_txt}"
            )

            if killed:
                print(f"tick {block} | KILL: {line_action}; {kill_txt}; stopping.")
                break

            n += 1
            _sleep_remaining(tick_start, limits.tick_seconds)

        return 0
    except (KeyboardInterrupt, _StopRequested):
        print(f"\ntick {block} | stopping: stop requested (Ctrl+C or Stop Trading Bot).")
        # Cancel unconditionally: `resting_quotes` only knows about this
        # run's quotes, and an order left open by anything else still fills.
        if not dry_execution:
            try:
                alpaca.cancel_all_orders()
                print(f"tick {block} | resting orders cancelled, shut down cleanly.")
            except AlpacaAPIError as exc:
                print(f"tick {block} | could not cancel resting orders cleanly: {exc}")
        else:
            print(f"tick {block} | shut down cleanly, dry run placed no orders.")
        return 0
    except Exception as exc:
        # Anything unexpected still goes out through a clean shutdown, so a
        # crash never leaves resting orders behind to fill unattended. The
        # exception is re-raised afterwards so it is logged and exits non-zero.
        print(f"\ntick {block} | stopping: unexpected error: {exc!r}")
        if not dry_execution:
            try:
                alpaca.cancel_all_orders()
                print(f"tick {block} | resting orders cancelled before exiting.")
            except Exception as cancel_exc:
                print(f"tick {block} | could not cancel resting orders: {cancel_exc}")
        raise
    finally:
        if previous_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)


def _read_top_of_book(
    alpaca, spec: AssetSpec
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Crypto: real L2 depth from the order book. Equities: best bid/ask
    from the latest quote only, wrapped in the same (price, size) shape so
    the rest of the loop never has to know the difference. Returns empty
    lists, never fabricated levels, when nothing is available."""
    if spec.has_depth:
        book = alpaca.get_orderbook()
        bids = [(float(l["p"]), float(l["s"])) for l in book.get("b", [])]
        asks = [(float(l["p"]), float(l["s"])) for l in book.get("a", [])]
        return bids, asks

    quote = alpaca.get_latest_quote()
    bids = (
        [(float(quote["bp"]), float(quote.get("bs", 0.0)))] if quote.get("bp") else []
    )
    asks = (
        [(float(quote["ap"]), float(quote.get("as", 0.0)))] if quote.get("ap") else []
    )
    return bids, asks


def _closed_market_record(block: int, now: float, symbol: str) -> dict:
    return {
        "tick": block,
        "ts": now,
        "symbol": symbol,
        "mid": None,
        "vwap": None,
        "spread_bps": None,
        "has_depth": False,
        "regime": None,
        "regime_conf": None,
        "direction": None,
        "toxic_flow": None,
        "liquidity_stressed": None,
        "quote_environment": None,
        "quote_environment_conf": None,
        "inventory_pressure": None,
        "execution_health": None,
        "action": "MARKET_CLOSED",
        "action_reason": "market closed, prices are stale",
        "direction_leg": None,
        "skew": 0.0,
        "rung": "hold_late",
        "late": False,
        "latency_ms": None,
        "model": None,
        "route": None,
        "inventory": 0.0,
        "unrealised_pnl_usd": 0.0,
        "drawdown_pct": 0.0,
        "fill": "-",
        "fill_qty": None,
        "fill_price": None,
    }


def _sync_inventory(inv: InventoryState, qty: float, avg_px: float, now: float) -> None:
    """Overwrite the loop's inventory with what the broker actually holds."""
    inv.inventory = qty
    if qty:
        inv.entry_price = avg_px
        if inv.position_opened_at is None:
            inv.position_opened_at = now
    else:
        inv.entry_price = 0.0
        inv.position_opened_at = None


def _position_change(prev: float | None, now: float) -> tuple[str | None, float | None]:
    """Side and size of a position change seen at the broker since the last
    tick, i.e. a resting quote that filled in between. (None, None) on the
    first tick or when nothing changed."""
    if prev is None:
        return None, None
    delta = now - prev
    if abs(delta) < 1e-9:
        return None, None
    return ("buy" if delta > 0 else "sell"), round(abs(delta), 10)


FILL_POLL_TICKS = 6  # fetch fills for the dashboard every ~30s at 5s ticks


def _new_fill_stats() -> dict:
    return {
        "buy": 0,
        "sell": 0,
        "buy_usd": 0.0,
        "sell_usd": 0.0,
        "buy_qty": 0.0,
        "sell_qty": 0.0,
        "pnl_usd": 0.0,
        "equity_change_usd": None,
    }


def _run_pnl(stats: dict, mid: float) -> float:
    """Trading P/L from this run's fills, marked at `mid`: cash in from sells
    minus cash out on buys, plus what the net quantity bought is worth now.
    Excludes fees and the drift on any position held before the run; the
    account's equity change (equity_change_usd) includes both."""
    net_qty = stats["buy_qty"] - stats["sell_qty"]
    return round(stats["sell_usd"] - stats["buy_usd"] + net_qty * mid, 2)


def _tally_fills(activities: list[dict], stats: dict, seen: set, symbol: str) -> str | None:
    """Add not-yet-counted fills for `symbol` to `stats`; return the latest
    transaction time seen (the cursor for the next fetch), or None."""
    want = symbol.replace("/", "")
    latest = None
    for a in activities:
        latest = a.get("transaction_time") or latest
        if a.get("id") in seen or (a.get("symbol") or "").replace("/", "") != want:
            continue
        seen.add(a.get("id"))
        side = a.get("side")
        if side not in ("buy", "sell"):
            continue
        qty = float(a.get("qty") or 0)
        stats[side] += 1
        stats[f"{side}_qty"] = round(stats[f"{side}_qty"] + qty, 10)
        # Unrounded dollars: rounding each fill to cents would drift the P/L.
        stats[f"{side}_usd"] += qty * float(a.get("price") or 0)
    return latest


def _sellable_qty(inventory: float, spec: AssetSpec) -> float:
    """How much can be sold without shorting: the held quantity, rounded
    down to the venue's precision. Unlimited where shorting is allowed."""
    if spec.shorting_allowed:
        return float("inf")
    step = 10**spec.qty_precision
    return int(max(0.0, inventory) * step) / step


def _kill_switch(alpaca, dry: bool) -> str:
    """Flatten and stop: cancel every resting order, then close the position
    at market. Orders are cancelled first because an open sell reserves the
    quantity the close needs; the close is retried briefly while those
    cancels settle. Returns a one-line summary for the tick log."""
    if dry:
        return "dry: would cancel all orders and close the position"
    notes = []
    try:
        alpaca.cancel_all_orders()
        notes.append("orders cancelled")
    except AlpacaAPIError as exc:
        notes.append(f"cancel failed: {exc}")
    for attempt in range(3):
        try:
            alpaca.close_position()
            notes.append("position closed at market")
            break
        except MarketClosedError as exc:
            notes.append(f"close skipped: {exc}")
            break
        except AlpacaAPIError as exc:
            if attempt == 2:
                notes.append(f"close failed: {exc}")
            else:
                time.sleep(0.7)
    return ", ".join(notes)


def _execute_action(
    *,
    alpaca,
    spec: AssetSpec,
    action,
    bid_px,
    ask_px,
    mid,
    quote_notional,
    directional_notional,
    snapshot,
    limits,
    inv,
    api_error_streak,
    decision_latency_ms,
    resting_quotes,
    rest_counter,
    now,
    open_buy_usd: float = 0.0,
    dry: bool = False,
):
    """Runs the risk check, then places (or, if `dry` is true, only logs)
    the order implied by `action`. `dry` never calls cancel_all_orders,
    submit_limit_order, or submit_market_order: it reads real market data
    and gets a real Jev battery answer, but never touches the Alpaca order
    book. Every fill line in dry mode starts with "dry:". Order sizes are
    computed from a dollar target (assets.size_order), not a fixed
    quantity, so the same limits work across any asset.

    Buys are only placed while they fit under max_position_usd together
    with the current position and any resting buys that stay open; a buy
    that does not fit is dropped (the sell side still quotes). The last
    element of the returned tuple is True when risk.check returned a KILL."""
    from .risk import check as risk_check

    quoting = action.kind in (QUOTE_BOTH_SIDES, QUOTE_WIDE)
    replacing = quoting and (resting_quotes is None or rest_counter + 1 >= limits.rest_ticks)

    # Headroom under the buy ceiling (a fraction of the hard cap). Resting
    # buys always count, even on a cancel-replace: Alpaca cancels
    # asynchronously, and a buy being cancelled can still fill.
    position_usd = abs(snapshot["inventory"]) * mid
    staying_buys_usd = open_buy_usd
    buy_ceiling = limits.max_position_usd * limits.buy_ceiling_fraction
    headroom = buy_ceiling - position_usd - staying_buys_usd
    place_buy_quote = replacing and quote_notional <= headroom
    new_buys_usd = quote_notional if place_buy_quote else 0.0
    leg_up_fits = directional_notional <= headroom - new_buys_usd
    if action.direction_leg == "up" and leg_up_fits:
        new_buys_usd += directional_notional

    order_notional_usd = max(quote_notional, directional_notional)
    verdict = risk_check(
        snapshot,
        order_notional_usd,
        limits,
        api_error_streak,
        decision_latency_ms,
        pending_buy_usd=staying_buys_usd + new_buys_usd,
        dust_usd=spec.min_notional_usd,
    )
    if not verdict.ok:
        if verdict.kill:
            return f"KILL ({verdict.veto})", "-", None, None, None, 0, True
        if open_buy_usd > 0 and not dry:
            # A veto must not leave resting buys behind: they would fill
            # anyway and carry the position past the cap.
            try:
                alpaca.cancel_all_orders()
                resting_quotes = None
            except AlpacaAPIError:
                pass
        return (
            f"VETOED ({verdict.veto})",
            "-",
            None,
            None,
            resting_quotes,
            rest_counter,
            False,
        )

    fill_txt = "-"
    line_action = action.kind

    # Reduce-only (position held past max_inventory_age_s): only the side
    # that shrinks the position may go out. Resting orders on the other
    # side are cancelled by forcing a cancel-replace this tick.
    leg = action.direction_leg
    reduce_only = verdict.reduce_only
    reducing_side = "sell" if snapshot["inventory"] > 0 else "buy"
    reduce_note = " (reduce-only: position held past max_inventory_age_s)" if reduce_only else ""
    if reduce_only:
        if leg == ("up" if reducing_side == "sell" else "down"):
            leg = None
        if reducing_side == "sell":
            place_buy_quote = False
            if quoting and open_buy_usd > 0:
                replacing = True

    if action.kind in (PULL_QUOTES, STAND_DOWN):
        if not dry:
            try:
                alpaca.cancel_all_orders()
            except AlpacaAPIError:
                pass
        return line_action, fill_txt, None, None, None, 0, False

    if action.kind == WIDEN:
        wide_bid, wide_ask = bid_px * 0.999, ask_px * 1.001
        return (
            f"{line_action} bid {wide_bid:,.1f}/ask {wide_ask:,.1f}",
            fill_txt,
            None,
            None,
            resting_quotes,
            rest_counter,
            False,
        )

    if quoting:
        fill_qty, fill_price = None, None
        buy_qty = size_order(quote_notional, bid_px, spec)
        sell_qty = size_order(quote_notional, ask_px, spec)
        # A cash account can only sell what it holds: trim the sell quote to
        # the position, and skip it when that is under the venue minimum.
        # Otherwise every sell is rejected and the quotes churn every tick.
        held = _sellable_qty(snapshot["inventory"], spec)
        sell_qty = min(sell_qty, held)
        place_sell_quote = sell_qty * ask_px >= spec.min_notional_usd
        if reduce_only and reducing_side == "buy":
            place_sell_quote = False
        if reduce_only:
            buy_note = reduce_note
        else:
            buy_note = "" if place_buy_quote or not replacing else " (buy side held: at position cap)"
        if replacing and not place_sell_quote:
            buy_note += " (sell side held: under the minimum order held)"
        # Gas-honesty rule: only cancel-replace every `rest_ticks` ticks.
        rest_counter += 1
        if replacing:
            if dry:
                resting_quotes = {"bid": bid_px, "ask": ask_px}
                rest_counter = 0
                buy_txt = f"{buy_qty}" if place_buy_quote else "-"
                sell_txt = f"{sell_qty}" if place_sell_quote else "-"
                fill_txt = f"dry: would quote {buy_txt}/{sell_txt} @ {bid_px:,.2f}/{ask_px:,.2f}"
            else:
                # Always cancel first: orders left by an earlier run are not
                # in `resting_quotes` but would still fill. Any venue
                # rejection is caught below and retried after `rest_ticks`,
                # not on every tick.
                try:
                    alpaca.cancel_all_orders()
                    resting_quotes = None
                    rest_counter = 0
                    if place_buy_quote:
                        alpaca.submit_limit_order("buy", buy_qty, bid_px)
                        resting_quotes = {"bid": bid_px}
                    if place_sell_quote:
                        alpaca.submit_limit_order("sell", sell_qty, ask_px)
                        resting_quotes = dict(resting_quotes or {}, ask=ask_px)
                    if resting_quotes is None:
                        resting_quotes = {}  # nothing to quote: wait rest_ticks, don't re-cancel every tick
                except MarketClosedError as exc:
                    return (
                        f"{line_action} ({exc})",
                        fill_txt,
                        None,
                        None,
                        resting_quotes,
                        rest_counter,
                        False,
                    )
                except AlpacaAPIError as exc:
                    return (
                        f"{line_action}{buy_note} (order error: {exc})",
                        fill_txt,
                        None,
                        None,
                        resting_quotes if resting_quotes is not None else {},
                        rest_counter,
                        False,
                    )

        if leg in ("up", "down"):
            side = "buy" if leg == "up" else "sell"
            if side == "buy" and not leg_up_fits:
                return (
                    f"{action.kind} skew {action.skew:+.1f}{buy_note} + buy leg held: at position cap",
                    fill_txt,
                    None,
                    None,
                    resting_quotes,
                    rest_counter,
                    False,
                )
            fill_px = bid_px if side == "sell" else ask_px
            leg_qty = size_order(directional_notional, fill_px, spec)
            if side == "sell":
                # Same cash-account rule: sell only what is held and not
                # already reserved by the sell quote placed this tick.
                reserved = sell_qty if (replacing and place_sell_quote) else 0.0
                leg_qty = min(leg_qty, _sellable_qty(snapshot["inventory"] - reserved, spec))
                if leg_qty * fill_px < spec.min_notional_usd:
                    return (
                        f"{action.kind} skew {action.skew:+.1f}{buy_note} + sell leg held: nothing to sell",
                        fill_txt,
                        None,
                        None,
                        resting_quotes,
                        rest_counter,
                        False,
                    )
            if dry:
                fill_txt = f"dry: would {side} {leg_qty} @ {fill_px:,.2f}"
                line_action = (
                    f"{action.kind} skew {action.skew:+.1f} + {side} leg (dry)"
                )
            else:
                try:
                    alpaca.submit_market_order(side, leg_qty)
                    inv.inventory += leg_qty if side == "buy" else -leg_qty
                    inv.fills += 1
                    inv.orders_submitted += 1
                    if inv.position_opened_at is None:
                        inv.position_opened_at = now
                    inv.entry_price = fill_px
                    record_fill_slippage(
                        inv, expected_price=mid, fill_price=fill_px, side=side
                    )
                    fill_qty = leg_qty
                    fill_price = fill_px
                    fill_txt = f"filled {fill_qty} @ {fill_px:,.2f}"
                    line_action = f"{action.kind} skew {action.skew:+.1f} + {side} leg"
                except MarketClosedError as exc:
                    fill_txt = f"leg skipped: {exc}"
                except AlpacaAPIError as exc:
                    inv.orders_rejected += 1
                    fill_txt = f"leg rejected: {exc}"
        else:
            line_action = f"{action.kind} skew {action.skew:+.1f}{buy_note}"

        return line_action, fill_txt, fill_qty, fill_price, resting_quotes, rest_counter, False

    return line_action, fill_txt, None, None, resting_quotes, rest_counter, False


def _sleep_remaining(tick_start: float, tick_seconds: float) -> None:
    elapsed = time.monotonic() - tick_start
    remaining = tick_seconds - elapsed
    if remaining > 0:
        time.sleep(remaining)


def _append_log(record: dict) -> None:
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _write_latest(
    symbol, block, ticks, meta, started_at, api_error_streak, fills: dict | None = None
) -> None:
    calls = len(ticks)
    late = sum(1 for t in ticks if t["action"] in ("HOLD_LATE", "MARKET_CLOSED"))
    latencies = [t["latency_ms"] for t in ticks if t.get("latency_ms")]
    avg_ms = sum(latencies) / len(latencies) if latencies else None
    payload = {
        "generated_at": time.time(),
        "symbol": symbol,
        "block": block,
        "ticks": ticks,
        "stats": {
            "avg_ms": avg_ms,
            "calls": calls,
            "late_count": late,
            "uptime_s": time.time() - started_at,
            "decision_client": meta.get("route"),
            "model": meta.get("model"),
            "api_error_streak": api_error_streak,
            "fills": fills,
        },
    }
    # The dashboard feed must never stop trading. On Windows the replace is
    # refused while another process holds latest.json open for a moment (the
    # dashboard server reading it, antivirus scanning it): retry briefly,
    # then skip this tick's update. Regression: 2026-09-24 09:03 a single
    # "Access is denied" here killed the loop without cancelling orders.
    tmp = LATEST_FILE.with_suffix(".tmp")
    for attempt in range(5):
        try:
            tmp.write_text(json.dumps(payload))
            tmp.replace(LATEST_FILE)
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
    print(f"tick {block} | dashboard feed not updated this tick: {last_exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop run")
    parser.add_argument(
        "--paper",
        action="store_true",
        default=True,
        help="paper mode (the default, and the only mode unless --live is used)",
    )
    parser.add_argument(
        "--mock", action="store_true", help="force the mock decision client"
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="stop after N ticks; 0 means run forever (same as --forever)",
    )
    parser.add_argument(
        "--forever",
        action="store_true",
        help="run continuously until stopped (Ctrl+C, or SIGTERM in the background), same as --ticks 0",
    )
    parser.add_argument("--symbol", default=os.environ.get("DEFAULT_SYMBOL", "BTC/USD"))
    parser.add_argument(
        "--dry-execution",
        action="store_true",
        help="real market data and a real Jev battery, but never submit an order to Alpaca",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "trade real money on Alpaca's live endpoint instead of paper. Also "
            "requires JEV_LOOP_ALLOW_LIVE=i-understand-the-risk in the environment "
            "and a typed confirmation at startup; missing either refuses to start. "
            "See SKILL.md / README.md before ever using this."
        ),
    )
    args = parser.parse_args(argv)

    ticks = None if (args.forever or args.ticks == 0) else args.ticks

    confirmation = None
    if args.live:
        print("\n" + "!" * 70)
        print("! --live was passed. This is not paper trading.")
        print("! Every order this places spends real money in your real Alpaca")
        print("! account, with no paper safety net.")
        print("!" * 70)
        try:
            confirmation = input(
                f"Type exactly '{LIVE_CONFIRMATION_PHRASE}' to continue, anything else cancels: "
            )
        except EOFError:
            confirmation = None

    limits = Limits()
    return run(
        symbol=args.symbol,
        ticks=ticks,
        mock=args.mock,
        limits=limits,
        dry_execution=args.dry_execution,
        live=args.live,
        confirmation=confirmation,
    )


if __name__ == "__main__":
    sys.exit(main())
