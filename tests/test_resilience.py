"""A side job failing (the dashboard feed, a network blip) must not stop
trading, and anything that does stop the loop must cancel resting orders
on the way out."""

from pathlib import Path

import pytest
import requests

from jevloop import loop
from jevloop.assets import resolve_symbol
from jevloop.execution.alpaca import AlpacaAPIError, AlpacaPaperClient
from jevloop.limits import Limits


@pytest.fixture
def tmp_log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "LOG_DIR", tmp_path)
    monkeypatch.setattr(loop, "LOG_FILE", tmp_path / "log.jsonl")
    monkeypatch.setattr(loop, "LATEST_FILE", tmp_path / "latest.json")
    monkeypatch.setattr(loop, "STOP_FILE", tmp_path / "stop.request")
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    return tmp_path


def _write(block=1):
    loop._write_latest("BTC/USD", block, [], {"route": None, "model": None}, 0.0, 0)


def test_dashboard_feed_retries_a_briefly_locked_file(tmp_log_dir, monkeypatch):
    # Regression (2026-09-24 09:03): one "Access is denied" killed the loop.
    real_replace = Path.replace
    failures = {"left": 2}

    def flaky_replace(self, target):
        if failures["left"]:
            failures["left"] -= 1
            raise PermissionError(5, "Access is denied")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    _write()
    assert (tmp_log_dir / "latest.json").exists()


def test_dashboard_feed_that_stays_locked_is_skipped_not_raised(tmp_log_dir, monkeypatch, capsys):
    def locked(self, target):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "replace", locked)
    _write(block=7)  # must not raise
    assert "tick 7 | dashboard feed not updated" in capsys.readouterr().out


def test_network_error_becomes_an_api_error():
    class DownSession:
        def request(self, *a, **k):
            raise requests.ConnectionError("connection reset")

    client = AlpacaPaperClient(api_key="x", secret_key="y", spec=resolve_symbol("BTC/USD"))
    client._session = DownSession()
    with pytest.raises(AlpacaAPIError) as info:
        client.get_account()
    assert info.value.status_code == 0


def test_unexpected_error_cancels_resting_orders_then_reraises(tmp_log_dir, monkeypatch):
    class FakeAlpaca:
        cancelled = 0

        def cancel_all_orders(self):
            FakeAlpaca.cancelled += 1

    monkeypatch.setattr(loop, "client_from_env", lambda **kwargs: FakeAlpaca())
    monkeypatch.setattr(loop, "resolve_decision_client", lambda mock=False: object())

    def boom(alpaca, spec):
        raise RuntimeError("something nobody expected")

    monkeypatch.setattr(loop, "_read_top_of_book", boom)
    with pytest.raises(RuntimeError):
        loop.run(symbol="BTC/USD", ticks=3, mock=True, limits=Limits())
    assert FakeAlpaca.cancelled == 1
