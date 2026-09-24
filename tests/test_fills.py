import json

from jevloop import loop
from jevloop.assets import resolve_symbol
from jevloop.execution.alpaca import AlpacaPaperClient
from jevloop.loop import _new_fill_stats, _tally_fills


def _fill(id, side, qty="0.0001", price="84000", symbol="BTC/USD", t="2026-09-24T13:50:00Z"):
    return dict(id=id, side=side, qty=qty, price=price, symbol=symbol, transaction_time=t)


def test_tally_counts_buys_and_sells_with_dollars():
    stats, seen = _new_fill_stats(), set()
    cursor = _tally_fills(
        [_fill("a", "buy"), _fill("b", "sell"), _fill("c", "sell", t="2026-09-24T13:51:00Z")],
        stats, seen, "BTC/USD",
    )
    assert (stats["buy"], stats["sell"]) == (1, 2)
    assert stats["buy_usd"] == 8.4 and stats["sell_usd"] == 16.8
    assert cursor == "2026-09-24T13:51:00Z"


def test_tally_never_counts_the_same_fill_twice():
    # Each fetch starts at the last transaction time, so the newest fill
    # comes back again in the next page.
    stats, seen = _new_fill_stats(), set()
    _tally_fills([_fill("a", "buy")], stats, seen, "BTC/USD")
    _tally_fills([_fill("a", "buy"), _fill("b", "buy")], stats, seen, "BTC/USD")
    assert stats["buy"] == 2


def test_tally_ignores_other_symbols_and_matches_either_spelling():
    stats, seen = _new_fill_stats(), set()
    _tally_fills(
        [_fill("a", "buy", symbol="ETHUSD"), _fill("b", "sell", symbol="BTCUSD")],
        stats, seen, "BTC/USD",
    )
    assert (stats["buy"], stats["sell"]) == (0, 1)


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = "x"

    def json(self):
        return self._payload


def test_get_fills_since_follows_pages_until_a_short_one():
    full = [dict(id=f"f{i}") for i in range(100)]
    pages = [full, [dict(id="last")]]
    seen_params = []

    class Session:
        def request(self, method, url, **kwargs):
            seen_params.append(dict(kwargs["params"]))
            return _Resp(pages.pop(0))

    client = AlpacaPaperClient(api_key="x", secret_key="y", spec=resolve_symbol("BTC/USD"))
    client._session = Session()
    fills = client.get_fills_since("2026-09-24T13:00:00Z")
    assert len(fills) == 101
    assert "page_token" not in seen_params[0]
    assert seen_params[1]["page_token"] == "f99"


def test_latest_json_carries_fill_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "LATEST_FILE", tmp_path / "latest.json")
    fills = dict(_new_fill_stats(), buy=3, sell=2)
    loop._write_latest("BTC/USD", 1, [], {"route": None, "model": None}, 0.0, 0, fills=fills)
    stats = json.loads((tmp_path / "latest.json").read_text())["stats"]
    assert stats["fills"]["buy"] == 3 and stats["fills"]["sell"] == 2
