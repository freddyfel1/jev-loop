"""The half-spread floor: quotes sit at least min_half_spread_frac of mid
away from the reservation price, so a round trip can cover two fees."""

import pytest

from jevloop.limits import Limits
from jevloop.pricing import half_spread, quote_prices

L = Limits()
MID = 84_000.0
AS = dict(sigma=0.001, gamma=L.as_gamma, kappa=L.as_kappa, time_left_s=L.as_horizon_s)


def test_without_a_floor_quotes_are_the_plain_a_s_spread():
    bid, ask = quote_prices(mid=MID, inventory=0.0, **AS)
    h = half_spread(L.as_gamma, 0.001, L.as_horizon_s, L.as_kappa)
    assert (bid, ask) == pytest.approx((MID - h, MID + h))
    assert (ask - bid) / MID < 0.001  # ~0.03% apart: far inside two fees


def test_floor_widens_quotes_to_the_configured_fraction():
    bid, ask = quote_prices(mid=MID, inventory=0.0, min_half_spread_frac=0.0025, **AS)
    assert bid == pytest.approx(MID * (1 - 0.0025))
    assert ask == pytest.approx(MID * (1 + 0.0025))
    assert (ask - bid) / MID == pytest.approx(0.005)


def test_default_floor_clears_a_round_trip_of_measured_fees():
    # jev-loop fees measured ~0.21% of each fill, so ~0.42% a round trip.
    assert 2 * L.min_half_spread_frac > 2 * 0.0021


def test_a_wider_a_s_spread_is_not_narrowed_by_the_floor():
    wild = dict(AS, sigma=10.0)  # sigma is in price units: volatile enough that A-S alone is wider
    h = half_spread(L.as_gamma, 10.0, L.as_horizon_s, L.as_kappa)
    assert h > MID * 0.0025
    bid, ask = quote_prices(mid=MID, inventory=0.0, min_half_spread_frac=0.0025, **wild)
    assert ask - bid == pytest.approx(2 * h)


def test_inventory_skew_still_applies_with_the_floor():
    flat = quote_prices(mid=MID, inventory=0.0, min_half_spread_frac=0.0025, **AS)
    long = quote_prices(mid=MID, inventory=5.0, min_half_spread_frac=0.0025, **dict(AS, sigma=0.05))
    assert sum(long) / 2 < sum(flat) / 2  # long inventory pulls both quotes down
