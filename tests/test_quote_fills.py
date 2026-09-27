"""The feed labels a tick BUY/SELL when a resting quote filled since the
last tick. Those fills happen at the broker between ticks, so the loop can
only see them as a change in the position it reads back every tick."""

from jevloop.loop import _position_change


def test_first_tick_reports_nothing():
    assert _position_change(None, 0.0002) == (None, None)


def test_unchanged_position_reports_nothing():
    assert _position_change(0.0002, 0.0002) == (None, None)


def test_float_noise_is_not_a_fill():
    assert _position_change(0.0002, 0.0002 + 1e-12) == (None, None)


def test_position_up_is_a_buy_fill():
    assert _position_change(0.0, 0.000238) == ("buy", 0.000238)


def test_position_down_is_a_sell_fill():
    assert _position_change(0.000439, 0.000201) == ("sell", 0.000238)


# --- fee settlement is not a fill ----------------------------------------
# Seen live 2026-09-26: a 0.0001185 BTC buy was followed a tick later by a
# 1.69e-7 BTC drop (the fee, ~1.4 cents) that the feed showed as a SELL.

PX = 84_000.0


def test_fee_sized_drop_is_not_labelled():
    assert _position_change(0.000118503, 0.000118334, price=PX, min_usd=1.0) == (None, None)


def test_real_fill_is_still_labelled_with_the_filter_on():
    assert _position_change(0.0, 0.000238, price=PX, min_usd=1.0) == ("buy", 0.000238)


def test_small_partial_fill_above_the_floor_is_labelled():
    # ~$2.52: below the $10 order minimum, but a real partial fill.
    assert _position_change(0.0, 0.00003, price=PX, min_usd=1.0) == ("buy", 0.00003)


def test_missing_price_does_not_hide_fills():
    assert _position_change(0.0, 0.000238, price=0.0, min_usd=1.0) == ("buy", 0.000238)
