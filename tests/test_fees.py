"""`jev-loop fees`: trading P/L versus fees, from Alpaca's FILL and CFEE
records. Record shapes below are copied from the paper account."""

import pytest

from jevloop.fees import _fee_usd, compute_report, render


def fill(side, qty, price, t="2026-09-24T12:00:00Z"):
    return {"side": side, "qty": str(qty), "price": str(price), "transaction_time": t, "symbol": "BTC/USD"}


USD_FEE = {  # taken in dollars on a sell
    "activity_type": "CFEE", "date": "2026-09-24", "net_amount": "-0.02",
    "description": "Coin Pair Transaction Fee (USD)", "currency": "USD",
}
COIN_FEE = {  # coin kept on a buy
    "activity_type": "CFEE", "date": "2026-09-24", "net_amount": "0",
    "description": "Coin Pair Transaction Fee (Non USD)", "qty": "-0.000000359", "price": "83703.53",
}


def test_fee_usd_reads_both_fee_kinds():
    assert _fee_usd(USD_FEE) == pytest.approx(0.02)
    assert _fee_usd(COIN_FEE) == pytest.approx(0.000000359 * 83703.53)


def test_round_trip_gross_and_implied_fees():
    # Buy 0.001 at 80,000, sell 0.001 at 80,010: $0.01 gross. Equity fell
    # $0.30, so fees must have been $0.31.
    fills = [fill("buy", 0.001, 80_000), fill("sell", 0.001, 80_010)]
    r = compute_report(fills, [], mark=80_000, equity=99_999.70)
    assert r["gross_pnl_usd"] == pytest.approx(0.01)
    assert r["equity_change_usd"] == pytest.approx(-0.30)
    assert r["implied_fees_usd"] == pytest.approx(0.31)
    assert r["volume_usd"] == pytest.approx(160.01)
    assert (r["fills_buy"], r["fills_sell"]) == (1, 1)


def test_open_position_is_valued_at_the_mark():
    # Bought and still holding: gross is the move since the buy.
    r = compute_report([fill("buy", 0.001, 80_000)], [], mark=81_000, equity=100_001.0)
    assert r["gross_pnl_usd"] == pytest.approx(1.0)
    assert r["implied_fees_usd"] == pytest.approx(0.0)


def test_cross_check_skips_the_last_day_with_fee_records():
    # Fees posted for the 24th and (partly) the 25th: only the 24th counts.
    fills = [
        fill("buy", 0.001, 80_000, "2026-09-24T10:00:00Z"),
        fill("sell", 0.001, 80_000, "2026-09-25T10:00:00Z"),
    ]
    fees = [dict(USD_FEE, date="2026-09-24", net_amount="-0.16"), dict(USD_FEE, date="2026-09-25")]
    r = compute_report(fills, fees, mark=80_000, equity=100_000)
    assert r["posted_days"] == ("2026-09-24", "2026-09-24")
    assert r["posted_fee_pct_of_volume"] == pytest.approx(0.16 / 80 * 100)


def test_no_fee_records_yet_means_no_cross_check():
    r = compute_report([fill("buy", 0.001, 80_000)], [], mark=80_000, equity=100_000)
    assert r["posted_fee_pct_of_volume"] is None
    assert "cross-check" not in render(r, "BTC/USD")


def test_render_shows_the_three_headline_numbers():
    fills = [fill("buy", 0.001, 80_000), fill("sell", 0.001, 80_010)]
    text = render(compute_report(fills, [], mark=80_000, equity=99_999.70), "BTC/USD")
    assert "+$0.01" in text and "-$0.31" in text and "-$0.30" in text
