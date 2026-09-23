import pytest

from jevloop import loop


def test_stop_file_raises_a_clean_stop_and_is_consumed(tmp_path, monkeypatch):
    stop = tmp_path / "stop.request"
    monkeypatch.setattr(loop, "STOP_FILE", stop)
    stop.touch()
    with pytest.raises(loop._StopRequested):
        loop._check_stop_file()
    assert not stop.exists()  # the next run must not stop straight away


def test_no_stop_file_means_keep_running(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "STOP_FILE", tmp_path / "stop.request")
    loop._check_stop_file()  # no exception
