"""`jev-loop fees`: what the paper account made from trading versus what it
paid Alpaca in fees, from the broker's own records. Read-only: it never
places or cancels an order.

Alpaca charges crypto fees two ways: on a sell it takes dollars ("Coin Pair
Transaction Fee (USD)"); on a buy it keeps a slice of the coin received
("... (Non USD)", a negative qty). Fee records are posted a day or more
after the trade, but the account equity already includes them. So the
report trusts equity for the total and shows the posted fees as a
cross-check on the fee rate:

    gross trading P/L = cash in from sells - cash out on buys
                        + net quantity bought, valued at the current price
    fees (implied)    = gross - (equity - starting equity)

That assumes the account only trades this one symbol and has had no
deposits or withdrawals since it opened.

With --since, equity can't be used: Alpaca's equity history leaves out
fees that haven't posted yet, so there is no honest equity value for the
start of the period. Alpaca posts one fee record per fill, in fill order,
so instead the period's fees are the posted records from that time on,
plus an estimate for the newest fills whose fee hasn't posted yet, at the
fee rate the account's own posted history shows. The estimate is labelled.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import defaultdict

DEFAULT_START_EQUITY = 100_000.0  # what a new Alpaca paper account starts with


def _fee_usd(f: dict) -> float:
    """Dollar cost of one CFEE record: coin kept on a buy (qty * price), or
    dollars taken on a sell (-net_amount)."""
    qty = float(f.get("qty") or 0.0)
    if qty:
        return abs(qty) * float(f.get("price") or 0.0)
    return -float(f.get("net_amount") or 0.0)


def compute_report(
    fills: list[dict],
    fees: list[dict],
    mark: float,
    equity: float,
    start_equity: float = DEFAULT_START_EQUITY,
) -> dict:
    """Pure: everything the report prints, from Alpaca's FILL and CFEE
    activity records, the current price and the account equity."""
    buy_usd = sell_usd = buy_qty = sell_qty = 0.0
    n_buy = n_sell = 0
    day_volume: dict[str, float] = defaultdict(float)
    day_fees: dict[str, float] = defaultdict(float)
    for f in fills:
        qty, px = float(f["qty"]), float(f["price"])
        day_volume[f["transaction_time"][:10]] += qty * px
        if f["side"] == "buy":
            n_buy += 1
            buy_usd += qty * px
            buy_qty += qty
        else:
            n_sell += 1
            sell_usd += qty * px
            sell_qty += qty
    for f in fees:
        day_fees[(f.get("date") or f.get("created_at") or "")[:10]] += _fee_usd(f)

    volume = buy_usd + sell_usd
    gross = sell_usd - buy_usd + (buy_qty - sell_qty) * mark
    equity_change = equity - start_equity
    implied_fees = gross - equity_change

    # Cross-check on days whose fee records have fully posted: every trading
    # day before the last day that has any fee record (that last day may
    # still be partly unposted).
    fee_days = sorted(d for d in day_fees if d)
    complete = sorted(d for d in day_volume if fee_days and d < fee_days[-1])
    posted_volume = sum(day_volume[d] for d in complete)
    posted_fees = sum(day_fees[d] for d in complete)

    n = n_buy + n_sell
    return {
        "fills_buy": n_buy,
        "fills_sell": n_sell,
        "volume_usd": volume,
        "gross_pnl_usd": gross,
        "equity": equity,
        "start_equity": start_equity,
        "equity_change_usd": equity_change,
        "implied_fees_usd": implied_fees,
        "gross_pct_of_volume": gross / volume * 100 if volume else 0.0,
        "fee_pct_of_volume": implied_fees / volume * 100 if volume else 0.0,
        "posted_fee_pct_of_volume": posted_fees / posted_volume * 100 if posted_volume else None,
        "posted_days": (complete[0], complete[-1]) if complete else None,
        "avg_fill_usd": volume / n if n else 0.0,
        "gross_per_fill_usd": gross / n if n else 0.0,
        "fee_per_fill_usd": implied_fees / n if n else 0.0,
        "first_fill": min((f["transaction_time"] for f in fills), default=None),
        "last_fill": max((f["transaction_time"] for f in fills), default=None),
    }


def posted_fee_rate(fills: list[dict], fees: list[dict]) -> float | None:
    """Fees as a fraction of traded value, over the fills whose fee has
    posted: with one fee record per fill posted in fill order, those are
    the oldest len(fees) fills."""
    ordered = sorted(fills, key=lambda f: f["transaction_time"])[: len(fees)]
    volume = sum(float(f["qty"]) * float(f["price"]) for f in ordered)
    return sum(_fee_usd(f) for f in fees) / volume if volume else None


def compute_since_report(fills: list[dict], fees: list[dict], mark: float, since: str) -> dict:
    """Pure: trading P/L and fees for fills at or after `since` (UTC, as
    YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS]). Trading P/L covers only the
    period's own fills (the net quantity they bought valued at `mark`), not
    price moves on a position already held when the period started."""
    rate = posted_fee_rate(fills, fees)
    win = sorted((f for f in fills if f["transaction_time"] >= since), key=lambda f: f["transaction_time"])
    win_fees = [f for f in fees if (f.get("created_at") or f.get("date") or "") >= since]
    unposted = win[len(win_fees):] if len(win) > len(win_fees) else []

    buy_usd = sum(float(f["qty"]) * float(f["price"]) for f in win if f["side"] == "buy")
    sell_usd = sum(float(f["qty"]) * float(f["price"]) for f in win if f["side"] == "sell")
    net_qty = sum(float(f["qty"]) * (1 if f["side"] == "buy" else -1) for f in win)
    volume = buy_usd + sell_usd
    gross = sell_usd - buy_usd + net_qty * mark
    posted = sum(_fee_usd(f) for f in win_fees)
    unposted_volume = sum(float(f["qty"]) * float(f["price"]) for f in unposted)
    estimated = unposted_volume * rate if rate is not None else 0.0
    fees_total = posted + estimated
    n = len(win)
    return {
        "since": since,
        "fills_buy": sum(1 for f in win if f["side"] == "buy"),
        "fills_sell": sum(1 for f in win if f["side"] == "sell"),
        "volume_usd": volume,
        "gross_pnl_usd": gross,
        "fees_posted_usd": posted,
        "fees_estimated_usd": estimated,
        "unposted_fills": len(unposted),
        "fee_rate": rate,
        "fees_usd": fees_total,
        "net_usd": gross - fees_total,
        "avg_fill_usd": volume / n if n else 0.0,
        "gross_per_fill_usd": gross / n if n else 0.0,
        "fee_per_fill_usd": fees_total / n if n else 0.0,
    }


def render_since(r: dict, symbol: str) -> str:
    lines = [
        f"fees report: {symbol}, Alpaca paper account, fills since {r['since']} UTC",
        f"  fills     {r['fills_buy']:,} buy / {r['fills_sell']:,} sell",
    ]
    if not r["fills_buy"] + r["fills_sell"]:
        lines.append("  no fills in this period yet.")
        return "\n".join(lines)
    lines += [
        f"  traded    ${r['volume_usd']:,.2f}  (avg fill ${r['avg_fill_usd']:,.2f})",
        "",
        f"  trading P/L before fees  {_money(r['gross_pnl_usd']):>12}",
        f"  fees                     {_money(-r['fees_usd']):>12}",
        f"  net                      {_money(r['net_usd']):>12}",
        "",
        f"  fees: {_money(-r['fees_posted_usd'])} posted"
        + (
            f", {_money(-r['fees_estimated_usd'])} estimated for {r['unposted_fills']} fill(s) "
            f"not posted yet, at the account's {r['fee_rate'] * 100:.3f}% rate"
            if r["unposted_fills"] and r["fee_rate"] is not None
            else ""
        ),
    ]
    if r["gross_per_fill_usd"] and r["fee_per_fill_usd"]:
        lines.append(
            f"  per fill: earns {r['gross_per_fill_usd'] * 100:+.2f} cents, pays "
            f"{r['fee_per_fill_usd'] * 100:.2f} cents in fees"
        )
    lines.append(
        "  trading P/L counts this period's fills only, valued at the current price; "
        "not price moves on a position held before it."
    )
    return "\n".join(lines)


SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$")


def _money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def render(r: dict, symbol: str) -> str:
    first, last = (r["first_fill"] or "-")[:10], (r["last_fill"] or "-")[:10]
    lines = [
        f"fees report: {symbol}, Alpaca paper account",
        f"  fills     {r['fills_buy']:,} buy / {r['fills_sell']:,} sell  ({first} to {last})",
        f"  traded    ${r['volume_usd']:,.2f}  (avg fill ${r['avg_fill_usd']:,.2f})",
        "",
        f"  trading P/L before fees  {_money(r['gross_pnl_usd']):>12}"
        f"   {r['gross_pct_of_volume']:+.3f}% of traded",
        f"  fees (implied)           {_money(-r['implied_fees_usd']):>12}"
        f"   {-r['fee_pct_of_volume']:+.3f}% of traded",
        f"  net = equity change      {_money(r['equity_change_usd']):>12}"
        f"   (equity ${r['equity']:,.2f} vs ${r['start_equity']:,.0f} start)",
        "",
    ]
    if r["posted_fee_pct_of_volume"] is not None:
        a, b = r["posted_days"]
        lines.append(
            f"  cross-check: fee records posted for {a} to {b} come to "
            f"{r['posted_fee_pct_of_volume']:.3f}% of that period's trading"
        )
    if r["gross_per_fill_usd"] > 0 and r["fee_per_fill_usd"] > 0:
        lines.append(
            f"  per fill: earns {r['gross_per_fill_usd'] * 100:.3f} cents, pays "
            f"{r['fee_per_fill_usd'] * 100:.2f} cents in fees "
            f"({r['fee_per_fill_usd'] / r['gross_per_fill_usd']:,.0f}x)"
        )
    lines.append(
        "  assumes one symbol and no deposits/withdrawals; "
        "fee records post with a delay, equity does not."
    )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    from .execution.alpaca import AlpacaAPIError, AlpacaConfigError, client_from_env

    ap = argparse.ArgumentParser(
        prog="jev-loop fees",
        description="Trading P/L versus Alpaca fees on the paper account (read-only).",
    )
    ap.add_argument("--symbol", default=os.environ.get("DEFAULT_SYMBOL", "BTC/USD"))
    ap.add_argument(
        "--start-equity",
        type=float,
        default=DEFAULT_START_EQUITY,
        help="account equity when it opened (default 100000, Alpaca's paper default)",
    )
    ap.add_argument(
        "--since",
        help="only fills from this UTC time on: YYYY-MM-DD or YYYY-MM-DDTHH:MM "
        "(e.g. 2026-09-27T13:36, when the wider quotes went live)",
    )
    a = ap.parse_args(argv)
    if a.since and not SINCE_RE.match(a.since):
        print(f"--since must look like 2026-09-27 or 2026-09-27T13:35 (UTC), got {a.since!r}")
        return 1
    try:
        # Paper only, and gentle on the rate limit: the trading loop shares
        # the account's Alpaca call budget and may be running right now.
        client = client_from_env(a.symbol, live=False, calls_per_minute=60)
        print("reading fills and fees from Alpaca (can take a minute)...")
        want = a.symbol.replace("/", "")
        fills = [
            f
            for f in client.get_activities("FILL")
            if (f.get("symbol") or "").replace("/", "") == want
        ]
        fees = client.get_activities("CFEE")
        equity = float(client.get_account()["equity"])
        mark = float(client.get_latest_trade().get("p") or 0.0)
    except (AlpacaConfigError, AlpacaAPIError) as exc:
        print(f"cannot read the account: {exc}")
        return 1
    if not fills:
        print(f"no {a.symbol} fills on this account yet.")
        return 0
    if a.since:
        print(render_since(compute_since_report(fills, fees, mark, a.since), a.symbol))
        return 0
    print(render(compute_report(fills, fees, mark, equity, a.start_equity), a.symbol))
    return 0
