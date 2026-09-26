"""The position cap has to hold against every way a position can grow:
resting quotes that fill between ticks, positions left over from an
earlier run, and open orders not yet filled. And a KILL has to actually
flatten and stop, not just print."""

import pytest

from jevloop.assets import resolve_symbol
from jevloop.execution.alpaca import AlpacaAPIError, AlpacaPaperClient
from jevloop.limits import Limits
from jevloop.loop import _execute_action, _kill_switch, _sync_inventory
from jevloop.policy import QUOTE_WIDE, Action
from jevloop.risk import check
from jevloop.state import InventoryState

L = Limits()
BTC = resolve_symbol("BTC/USD")
MID = 84_000.0


class FakeAlpaca:
    def __init__(self, fail_close_times: int = 0):
        self.calls: list[tuple] = []
        self._fail_close_times = fail_close_times

    def cancel_all_orders(self):
        self.calls.append(("cancel_all",))

    def submit_limit_order(self, side, qty, price):
        self.calls.append(("limit", side))

    def submit_market_order(self, side, qty):
        self.calls.append(("market", side))

    def close_position(self):
        if self._fail_close_times:
            self._fail_close_times -= 1
            raise AlpacaAPIError(403, "insufficient qty available")
        self.calls.append(("close",))


def _snapshot(position_usd: float, position_age_s: float = 10.0) -> dict:
    return dict(
        drawdown_pct=0.0,
        inventory=position_usd / MID,
        mid=MID,
        daily_loss_usd=0.0,
        position_age_s=position_age_s,
        data_age_s=0.2,
        leverage=1.0,
    )


def _execute(
    alpaca, position_usd, open_buy_usd=0.0, leg=None, resting=None, rest_counter=0, age_s=10.0
):
    return _execute_action(
        alpaca=alpaca,
        spec=BTC,
        action=Action(kind=QUOTE_WIDE, reason="test", direction_leg=leg),
        bid_px=MID - 5,
        ask_px=MID + 5,
        mid=MID,
        quote_notional=L.quote_notional_usd,
        directional_notional=L.directional_notional_usd,
        snapshot=_snapshot(position_usd, age_s),
        limits=L,
        inv=InventoryState(inventory=position_usd / MID),
        api_error_streak=0,
        decision_latency_ms=400.0,
        resting_quotes=resting,
        rest_counter=rest_counter,
        now=0.0,
        open_buy_usd=open_buy_usd,
    )


# --- risk.check: pending buys count toward the cap -----------------------

def test_pending_buys_default_to_zero_so_old_callers_are_unchanged():
    assert check(_snapshot(20.0), 20.0, L, 0, 90.0).ok


def test_pending_buys_that_would_breach_the_cap_veto_without_kill():
    v = check(_snapshot(20.0), 20.0, L, 0, 90.0, pending_buy_usd=40.0)
    assert not v.ok and not v.kill
    assert "pending buys" in v.veto


def test_position_already_over_the_cap_still_kills():
    v = check(_snapshot(144.0), 20.0, L, 0, 90.0)
    assert not v.ok and v.kill


# --- regression: a quote fill the loop never placed as a market leg ------

def test_quote_fill_seen_at_the_broker_counts_toward_the_cap():
    # The loop thinks it is flat, but resting quotes filled between ticks
    # and the broker now holds $60. Reconciling must make the cap trip.
    inv = InventoryState(inventory=0.0)
    _sync_inventory(inv, qty=60.0 / MID, avg_px=MID, now=100.0)
    snap = _snapshot(inv.inventory * MID)
    v = check(snap, 20.0, L, 0, 90.0)
    assert not v.ok and v.kill


def test_sync_inventory_overwrites_and_resets_on_flat():
    inv = InventoryState(inventory=0.5, entry_price=1.0)
    _sync_inventory(inv, qty=0.001, avg_px=84_391.7, now=100.0)
    assert inv.inventory == 0.001
    assert inv.entry_price == 84_391.7
    assert inv.position_opened_at == 100.0
    _sync_inventory(inv, qty=0.0, avg_px=0.0, now=200.0)
    assert inv.inventory == 0.0 and inv.position_opened_at is None


# --- _execute_action: buys only while they fit under the cap -------------

def test_both_sides_quote_with_headroom_and_orders_are_cancelled_first():
    alpaca = FakeAlpaca()
    *_, killed = _execute(alpaca, position_usd=20.0)
    assert not killed
    assert alpaca.calls == [("cancel_all",), ("limit", "buy"), ("limit", "sell")]


# --- cash account: sell only what is held --------------------------------

def test_flat_position_quotes_the_buy_side_only_instead_of_a_rejected_sell():
    # Regression (2026-09-23 18:02): every tick's $20 sell was rejected for
    # insufficient balance while holding ~$10, and the quotes churned.
    alpaca = FakeAlpaca()
    line, *_ = _execute(alpaca, position_usd=0.0)
    assert alpaca.calls == [("cancel_all",), ("limit", "buy")]
    assert "sell side held" in line


def test_sell_quote_is_trimmed_to_what_is_held():
    alpaca = FakeAlpaca()
    alpaca.submit_limit_order = lambda side, qty, price: alpaca.calls.append(("limit", side, qty))
    _execute(alpaca, position_usd=15.0)
    sells = [c for c in alpaca.calls if c[:2] == ("limit", "sell")]
    assert sells and sells[0][2] <= 15.0 / MID


def test_holding_under_the_minimum_skips_the_sell_quote():
    alpaca = FakeAlpaca()
    _execute(alpaca, position_usd=9.0)
    assert ("limit", "sell") not in alpaca.calls


def test_down_leg_is_held_when_the_sell_quote_already_reserves_the_holding():
    alpaca = FakeAlpaca()
    line, *_ = _execute(alpaca, position_usd=20.0, leg="down")
    assert ("limit", "sell") in alpaca.calls
    assert ("market", "sell") not in alpaca.calls
    assert "sell leg held" in line


def test_nothing_to_quote_does_not_re_cancel_every_tick():
    # At the ceiling with nothing sellable, the next tick must wait
    # rest_ticks instead of cancel-replacing again.
    alpaca = FakeAlpaca()
    *_, resting, rest_counter, _ = _execute(alpaca, position_usd=0.0)
    assert resting is not None


def test_cancel_happens_even_when_this_run_has_no_resting_quotes():
    # Orders left open by an earlier run are invisible to `resting_quotes`.
    alpaca = FakeAlpaca()
    _execute(alpaca, position_usd=0.0, resting=None)
    assert alpaca.calls[0] == ("cancel_all",)


def test_buy_quote_is_dropped_when_it_would_breach_the_cap():
    alpaca = FakeAlpaca()
    line, *_, killed = _execute(alpaca, position_usd=40.0)
    assert not killed
    assert ("limit", "buy") not in alpaca.calls
    assert ("limit", "sell") in alpaca.calls
    assert "buy side held" in line


def test_up_leg_is_held_when_no_room_is_left():
    alpaca = FakeAlpaca()
    line, *_ = _execute(alpaca, position_usd=15.0, leg="up")
    # $15 + $20 buy quote leaves $15 of room: not enough for a $20 leg.
    assert ("limit", "buy") in alpaca.calls
    assert ("market", "buy") not in alpaca.calls
    assert "buy leg held" in line


def test_down_leg_still_trades_at_the_cap():
    alpaca = FakeAlpaca()
    _execute(alpaca, position_usd=45.0, leg="down")
    assert ("market", "sell") in alpaca.calls


def test_resting_buys_that_stay_open_count_between_replaces():
    # Mid-rest (no cancel-replace this tick): $25 position + $20 resting buy
    # leaves $5, so an up leg must be held.
    alpaca = FakeAlpaca()
    line, *_ = _execute(
        alpaca, position_usd=25.0, open_buy_usd=20.0, leg="up",
        resting={"bid": MID - 5, "ask": MID + 5}, rest_counter=0,
    )
    assert alpaca.calls == []
    assert "buy leg held" in line


def test_buys_stop_at_the_ceiling_below_the_hard_cap():
    # $30 + $20 = $50 would be exactly the cap, but new buys stop at 90%
    # ($45) so a small price rise can't turn a full position into a KILL.
    alpaca = FakeAlpaca()
    line, *_ = _execute(alpaca, position_usd=30.0)
    assert ("limit", "buy") not in alpaca.calls
    assert "buy side held" in line


def test_buys_being_cancelled_still_count_on_a_cancel_replace():
    # Regression (2026-09-23 tick 326-328): a $10 buy cancelled during a
    # cancel-replace filled anyway while its replacement was already open.
    # Resting buys must count even when this tick is replacing them.
    alpaca = FakeAlpaca()
    _execute(alpaca, position_usd=20.0, open_buy_usd=20.0, resting=None)
    assert ("limit", "buy") not in alpaca.calls
    assert ("limit", "sell") in alpaca.calls


def test_veto_cancels_resting_buys_so_they_cannot_fill_past_the_cap():
    # Regression (tick 327): position $45 plus a $10 resting buy was vetoed,
    # but the buy stayed open, filled, and the next tick was a KILL.
    alpaca = FakeAlpaca()
    line, *_, killed = _execute(
        alpaca, position_usd=45.0, open_buy_usd=10.0,
        resting={"bid": MID - 5, "ask": MID + 5}, rest_counter=0,
    )
    assert line.startswith("VETOED") and not killed
    assert alpaca.calls == [("cancel_all",)]


def test_position_over_the_cap_returns_killed():
    alpaca = FakeAlpaca()
    line, *_, killed = _execute(alpaca, position_usd=144.0)
    assert killed and line.startswith("KILL")
    assert alpaca.calls == []  # the loop runs the kill switch, not _execute_action


# --- the kill switch actually flattens -----------------------------------

def test_kill_switch_cancels_then_closes():
    alpaca = FakeAlpaca()
    txt = _kill_switch(alpaca, dry=False)
    assert alpaca.calls == [("cancel_all",), ("close",)]
    assert "position closed" in txt


def test_kill_switch_retries_close_while_cancels_settle(monkeypatch):
    monkeypatch.setattr("jevloop.loop.time.sleep", lambda s: None)
    alpaca = FakeAlpaca(fail_close_times=1)
    txt = _kill_switch(alpaca, dry=False)
    assert ("close",) in alpaca.calls
    assert "position closed" in txt


def test_kill_switch_reports_a_close_that_keeps_failing(monkeypatch):
    monkeypatch.setattr("jevloop.loop.time.sleep", lambda s: None)
    alpaca = FakeAlpaca(fail_close_times=5)
    txt = _kill_switch(alpaca, dry=False)
    assert "close failed" in txt


def test_kill_switch_dry_touches_nothing():
    alpaca = FakeAlpaca()
    txt = _kill_switch(alpaca, dry=True)
    assert alpaca.calls == [] and txt.startswith("dry:")


# --- Alpaca client: position and open-order reads ------------------------

class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = "x" if payload is not None else ""

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.seen = []

    def request(self, method, url, **kwargs):
        self.seen.append((method, url, kwargs.get("params")))
        return self.responses.pop(0)


def _client(responses):
    c = AlpacaPaperClient(api_key="x", secret_key="y", spec=BTC)
    c._session = FakeSession(responses)
    return c


def test_get_position_reads_the_broker_and_treats_404_as_flat():
    c = _client([
        FakeResponse(200, {"qty": "0.001712312", "side": "long", "avg_entry_price": "84391.7"}),
        FakeResponse(404, {"message": "position does not exist"}),
    ])
    assert c.get_position() == (0.001712312, 84391.7)
    assert c.get_position() == (0.0, 0.0)
    assert c._session.seen[0][1].endswith("/v2/positions/BTCUSD")


def test_open_buy_notional_counts_only_unfilled_buy_limits():
    c = _client([FakeResponse(200, [
        {"side": "buy", "qty": "0.0002", "filled_qty": "0.0001", "limit_price": "84000"},
        {"side": "sell", "qty": "0.0002", "filled_qty": "0", "limit_price": "84010"},
        {"side": "buy", "qty": "0.0001", "filled_qty": "0", "limit_price": None},
    ])])
    assert c.get_open_buy_notional_usd() == pytest.approx(8.4)
    assert c._session.seen[0][2]["status"] == "open"


def test_close_position_treats_404_as_already_flat():
    c = _client([FakeResponse(404, {"message": "position does not exist"})])
    c.close_position()  # no exception


# --- inventory age: reduce-only, never a full block ----------------------
# Regression (2026-09-26): a $17 BTC position held past 15 minutes vetoed
# every order, including the sell that would have cleared it, so the loop
# sat on QUOTING with no orders for hours.

STALE = L.max_inventory_age_s + 1


def test_stale_position_is_reduce_only_not_vetoed():
    v = check(_snapshot(20.0, STALE), 20.0, L, 0, 90.0, dust_usd=BTC.min_notional_usd)
    assert v.ok and v.reduce_only and not v.kill


def test_stale_dust_under_the_venue_minimum_is_not_reduce_only():
    v = check(_snapshot(5.0, STALE), 20.0, L, 0, 90.0, dust_usd=BTC.min_notional_usd)
    assert v.ok and not v.reduce_only


def test_stale_long_quotes_the_sell_side_only():
    alpaca = FakeAlpaca()
    line, *_, killed = _execute(alpaca, position_usd=20.0, age_s=STALE)
    assert not killed
    assert alpaca.calls == [("cancel_all",), ("limit", "sell")]
    assert "reduce-only" in line


def test_stale_long_drops_the_up_leg_but_keeps_the_down_leg():
    alpaca = FakeAlpaca()
    _execute(alpaca, position_usd=20.0, age_s=STALE, leg="up")
    assert ("market", "buy") not in alpaca.calls
    # Resting quotes not due for replacement, so no new sell quote reserves
    # the holding and the down leg can sell it.
    alpaca = FakeAlpaca()
    _execute(alpaca, position_usd=20.0, age_s=STALE, leg="down", resting={"ask": MID}, rest_counter=0)
    assert ("market", "sell") in alpaca.calls


def test_stale_long_cancels_resting_buys_even_between_replacements():
    alpaca = FakeAlpaca()
    _execute(alpaca, position_usd=20.0, age_s=STALE, open_buy_usd=20.0, resting={"bid": MID, "ask": MID}, rest_counter=0)
    assert alpaca.calls[0] == ("cancel_all",)
    assert ("limit", "buy") not in alpaca.calls
