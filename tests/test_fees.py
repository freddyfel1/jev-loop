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


# --- --since: posted fees plus an estimate for fills not posted yet --------

from jevloop.fees import SINCE_RE, compute_since_report, posted_fee_rate, render_since


def fee(t, amount):
    return dict(USD_FEE, created_at=t, net_amount=str(-amount))


def test_posted_fee_rate_uses_the_oldest_fills_one_fee_each():
    fills = [fill("buy", 0.001, 80_000, "2026-09-24T10:00:00Z"), fill("sell", 0.001, 80_000, "2026-09-24T11:00:00Z")]
    # One fee posted so far: it belongs to the older, $80 fill.
    assert posted_fee_rate(fills, [fee("2026-09-24T10:00:01Z", 0.16)]) == pytest.approx(0.002)


def test_since_only_counts_fills_and_fees_in_the_period():
    fills = [
        fill("buy", 0.001, 80_000, "2026-09-24T10:00:00Z"),   # before: ignored
        fill("buy", 0.001, 80_000, "2026-09-27T14:00:00Z"),
        fill("sell", 0.001, 80_400, "2026-09-27T15:00:00Z"),  # +$0.40 round trip
    ]
    fees = [fee("2026-09-24T10:00:01Z", 0.16), fee("2026-09-27T14:00:01Z", 0.16), fee("2026-09-27T15:00:01Z", 0.16)]
    r = compute_since_report(fills, fees, mark=80_000, since="2026-09-27T13:35")
    assert (r["fills_buy"], r["fills_sell"]) == (1, 1)
    assert r["gross_pnl_usd"] == pytest.approx(0.40)
    assert r["fees_posted_usd"] == pytest.approx(0.32)
    assert r["unposted_fills"] == 0
    assert r["net_usd"] == pytest.approx(0.08)


def test_unposted_fills_get_an_estimated_fee_at_the_posted_rate():
    fills = [
        fill("buy", 0.001, 80_000, "2026-09-24T10:00:00Z"),
        fill("buy", 0.001, 80_000, "2026-09-27T14:00:00Z"),   # fee not posted yet
    ]
    r = compute_since_report(fills, [fee("2026-09-24T10:00:01Z", 0.16)], mark=80_000, since="2026-09-27")
    assert r["unposted_fills"] == 1
    assert r["fees_estimated_usd"] == pytest.approx(0.16)  # $80 at the 0.2% rate
    assert "estimated for 1 fill" in render_since(r, "BTC/USD")


def test_since_with_no_fills_yet_says_so():
    r = compute_since_report([fill("buy", 0.001, 80_000, "2026-09-24T10:00:00Z")], [], mark=80_000, since="2026-09-27")
    assert "no fills in this period yet" in render_since(r, "BTC/USD")


@pytest.mark.parametrize("ok", ["2026-09-27", "2026-09-27T13:35", "2026-09-27T13:35:50"])
def test_since_accepts_dates_and_times(ok):
    assert SINCE_RE.match(ok)


@pytest.mark.parametrize("bad", ["27/09/2026", "2026-9-27", "yesterday", "2026-09-27 13:35"])
def test_since_rejects_other_formats(bad):
    assert not SINCE_RE.match(bad)
