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
