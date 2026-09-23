import json

import pytest

from jevloop import calibrate
from jevloop.calibrate import (
    brier_skill,
    choose_model,
    pair_predictions,
    split_runs,
    unchanged_share,
)


def _tick(tick, mid, p_up=0.6, model="typesafe-ai/jev", run_id="r1", **extra):
    return dict(tick=tick, mid=mid, p_up=p_up, model=model, run_id=run_id, **extra)


def test_scores_jevs_own_p_up_not_another_answers_confidence():
    run = [
        _tick(1, 100.0, p_up=0.9, quote_environment_conf=0.1),
        _tick(2, 101.0, p_up=0.2, quote_environment_conf=0.9),
    ]
    assert pair_predictions(run, horizon=1) == [(0.9, 1)]


def test_ticks_without_p_up_are_skipped_not_filled_in():
    run = [_tick(1, 100.0, p_up=None, direction="up"), _tick(2, 101.0)]
    assert pair_predictions(run, horizon=1) == []


def test_unchanged_price_is_not_up_and_is_counted_as_unchanged():
    run = [_tick(1, 100.0), _tick(2, 100.0)]
    assert pair_predictions(run, horizon=1) == [(0.6, 0)]
    assert unchanged_share(run, horizon=1) == (1, 1)


def test_pairs_never_cross_runs():
    ticks = [_tick(1, 100.0, run_id="a"), _tick(1, 200.0, run_id="b")]
    runs = split_runs(ticks)
    assert len(runs) == 2
    assert all(pair_predictions(r, horizon=1) == [] for r in runs)


def test_old_logs_without_run_id_split_where_the_tick_counter_restarts():
    ticks = [
        dict(tick=1, mid=1.0), dict(tick=2, mid=1.0), dict(tick=3, mid=1.0),
        dict(tick=1, mid=2.0), dict(tick=2, mid=2.0),
    ]
    assert [len(r) for r in split_runs(ticks)] == [3, 2]


def test_default_model_is_the_latest_real_one_never_the_mock():
    runs = split_runs([
        _tick(1, 1.0, model="typesafe-ai/jev", run_id="a"),
        _tick(1, 1.0, model="mock-jev-0.1", run_id="b"),
    ])
    assert choose_model(runs, None) == "typesafe-ai/jev"
    assert choose_model(runs, "mock-jev-0.1") == "mock-jev-0.1"


def test_no_model_when_only_old_ticks_without_p_up_exist():
    runs = split_runs([dict(tick=1, mid=1.0, model="typesafe-ai/jev")])
    assert choose_model(runs, None) is None


def test_skill_is_zero_for_always_predicting_the_base_rate():
    pairs = [(0.25, 1), (0.25, 0), (0.25, 0), (0.25, 0)]
    base, skill = brier_skill(pairs)
    assert base == 0.25
    assert skill == pytest.approx(0.0)


def test_skill_is_positive_for_informative_predictions():
    pairs = [(0.9, 1), (0.1, 0), (0.8, 1), (0.2, 0)]
    _, skill = brier_skill(pairs)
    assert skill > 0.5


def test_main_scores_only_the_chosen_model(tmp_path, monkeypatch, capsys):
    log = tmp_path / "log.jsonl"
    lines = [_tick(i, 100.0 + i, run_id="real") for i in range(1, 6)]
    lines += [_tick(i, 100.0 - i, model="mock-jev-0.1", run_id="mock") for i in range(1, 6)]
    log.write_text("\n".join(json.dumps(t) for t in lines) + "\n")
    monkeypatch.setattr(calibrate, "LOG_FILE", log)
    monkeypatch.setattr(calibrate, "LOG_DIR", tmp_path)
    assert calibrate.main(["--horizon", "1"]) == 0
    out = capsys.readouterr().out
    assert "model typesafe-ai/jev: 4 decisions over 1 run(s)" in out
    assert "price higher after 1 ticks: 100%" in out
