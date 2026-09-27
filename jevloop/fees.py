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
"""

from __future__ import annotations

import argparse
import os
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
    a = ap.parse_args(argv)
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
    print(render(compute_report(fills, fees, mark, equity, a.start_equity), a.symbol))
    return 0
