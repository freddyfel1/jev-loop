"""Calibration: does 80% mean 80% on this venue?

Reads the tick log, pairs Jev's own P(up) from each tick's direction
answer with whether the price was actually higher N ticks later, and
reports a Brier score, a skill score against always predicting the base
rate, and a 10-bin reliability table. Writes reliability.png if
matplotlib happens to be installed; otherwise the table alone is enough.

Scored honestly:
- only ticks that logged Jev's real P(up) (`p_up`) count; older log lines
  without it are skipped, never filled in from some other answer's
  confidence;
- one model at a time (the most recent by default, never the mock unless
  asked for), so a mock run can't flatter or sink a real model's score;
- pairs never cross runs: a tick is only compared with a later tick from
  the same run;
- "up" means the mid was strictly higher; an unchanged price is not up,
  and the share of unchanged outcomes is reported so a quiet market is
  visible rather than silently scored.

This is not "did Jev predict price" in isolation. It is the honest check
the article insists on: if the model says 80%, does 80% actually happen.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path(__file__).resolve().parent.parent / "data")))
LOG_FILE = LOG_DIR / "log.jsonl"
DEFAULT_HORIZON = 30  # ticks: one minute at the default 2s tick


def load_ticks() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    ticks = []
    with LOG_FILE.open() as f:
        for line in f:
            line = line.strip()
            if line:
                ticks.append(json.loads(line))
    return ticks


def split_runs(ticks: list[dict]) -> list[list[dict]]:
    """Group log lines into runs: by `run_id` where logged, otherwise by the
    tick counter restarting (older logs, written before run_id existed)."""
    runs: list[list[dict]] = []
    for t in ticks:
        if runs:
            prev = runs[-1][-1]
            same_id = t.get("run_id") is not None and t.get("run_id") == prev.get("run_id")
            no_ids = t.get("run_id") is None and prev.get("run_id") is None
            if same_id or (no_ids and t.get("tick", 0) > prev.get("tick", 0)):
                runs[-1].append(t)
                continue
        runs.append([t])
    return runs


def run_model(run: list[dict]) -> str | None:
    """The model that answered this run (the last one logged), or None."""
    models = [t.get("model") for t in run if t.get("model")]
    return models[-1] if models else None


def pair_predictions(run: list[dict], horizon: int = DEFAULT_HORIZON) -> list[tuple[float, int]]:
    """(Jev's P(up), 1 if the mid was higher `horizon` ticks later) for one
    run. Ticks without a logged `p_up` are skipped."""
    pairs = []
    for i, t in enumerate(run):
        p_up = t.get("p_up")
        j = i + horizon
        if p_up is None or j >= len(run):
            continue
        pairs.append((float(p_up), 1 if run[j]["mid"] > t["mid"] else 0))
    return pairs


def unchanged_share(run: list[dict], horizon: int = DEFAULT_HORIZON) -> tuple[int, int]:
    """(scored ticks whose mid was unchanged `horizon` ticks later, scored ticks)."""
    flat = total = 0
    for i, t in enumerate(run):
        j = i + horizon
        if t.get("p_up") is None or j >= len(run):
            continue
        total += 1
        flat += run[j]["mid"] == t["mid"]
    return flat, total


def brier_score(pairs: list[tuple[float, int]]) -> float:
    if not pairs:
        return float("nan")
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def brier_skill(pairs: list[tuple[float, int]]) -> tuple[float, float]:
    """(base rate of up, skill vs always predicting that base rate).
    Skill > 0 beats the base rate, 0 matches it, < 0 is worse."""
    if not pairs:
        return float("nan"), float("nan")
    base = sum(y for _, y in pairs) / len(pairs)
    reference = brier_score([(base, y) for _, y in pairs])
    if reference == 0:
        return base, float("nan")
    return base, 1 - brier_score(pairs) / reference


def reliability_table(pairs: list[tuple[float, int]], n_bins: int = 10) -> list[dict]:
    bins = [[] for _ in range(n_bins)]
    for p, y in pairs:
        idx = min(n_bins - 1, int(p * n_bins))
        bins[idx].append((p, y))
    rows = []
    for i, b in enumerate(bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        if b:
            mean_pred = sum(p for p, _ in b) / len(b)
            empirical = sum(y for _, y in b) / len(b)
        else:
            mean_pred, empirical = float("nan"), float("nan")
        rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(b), "mean_predicted": mean_pred, "empirical": empirical})
    return rows


def choose_model(runs: list[list[dict]], requested: str | None) -> str | None:
    """The requested model, else the most recent model that logged p_up and
    is not the mock."""
    if requested:
        return requested
    for run in reversed(runs):
        model = run_model(run)
        if model and not model.startswith("mock") and any(t.get("p_up") is not None for t in run):
            return model
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop calibrate")
    parser.add_argument(
        "--horizon",
        type=int,
        default=DEFAULT_HORIZON,
        help=f"ticks ahead to check the realised outcome (default {DEFAULT_HORIZON}, one minute at 2s ticks)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model to score, e.g. typesafe-ai/jev or mock-jev-0.1 (default: most recent non-mock model)",
    )
    args = parser.parse_args(argv)

    ticks = load_ticks()
    if not ticks:
        print(f"No log found at {LOG_FILE}. Run `jev-loop run --ticks 60` first.")
        return 1

    runs = split_runs(ticks)
    model = choose_model(runs, args.model)
    if model is None:
        print(
            "No ticks with Jev's P(up) logged yet (older log lines predate it, and the "
            "mock is only scored with --model mock-jev-0.1). Run the loop for a while first."
        )
        return 1

    model_runs = [r for r in runs if run_model(r) == model]
    pairs = [p for r in model_runs for p in pair_predictions(r, args.horizon)]
    if not pairs:
        print(
            f"Not enough {model} ticks with P(up) logged to look {args.horizon} ticks ahead "
            "yet. Run a longer session."
        )
        return 1

    flat = total = 0
    for r in model_runs:
        f, t = unchanged_share(r, args.horizon)
        flat += f
        total += t
    score = brier_score(pairs)
    base, skill = brier_skill(pairs)

    print(f"\nmodel {model}: {len(pairs)} decisions over {len(model_runs)} run(s), horizon {args.horizon} ticks")
    print(f"Brier score: {score:.4f} (0 = perfect, 0.25 = coin flip, 1 = always wrong)")
    print(f"price higher after {args.horizon} ticks: {base:.0%} of the time (unchanged: {flat / total:.0%})")
    print(
        f"skill vs always predicting {base:.0%}: {skill:+.3f} "
        "(> 0 beats the base rate, 0 matches it, < 0 is worse)\n"
    )
    print(f"{'bin':>10} {'n':>5} {'mean predicted':>15} {'empirical':>10}")
    rows = reliability_table(pairs)
    for row in rows:
        mp = f"{row['mean_predicted']:.2f}" if row["n"] else "-"
        emp = f"{row['empirical']:.2f}" if row["n"] else "-"
        print(f"{row['bin']:>10} {row['n']:>5} {mp:>15} {emp:>10}")

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        filled = [r for r in rows if r["n"]]
        fig, ax = plt.subplots(figsize=(5, 5))
        ax.plot([0, 1], [0, 1], linestyle="--", color="gray", label="perfectly calibrated")
        ax.plot(
            [r["mean_predicted"] for r in filled],
            [r["empirical"] for r in filled],
            marker="o",
            label=f"{model} ({len(pairs)} decisions)",
        )
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_xlabel("predicted P(up)")
        ax.set_ylabel(f"share higher after {args.horizon} ticks")
        ax.set_title("Reliability: does 80% mean 80%?")
        ax.legend()
        out = LOG_DIR / "reliability.png"
        fig.savefig(out, dpi=150, bbox_inches="tight")
        print(f"\nwrote {out}")
    except ImportError:
        print("\n(matplotlib not installed, skipping reliability.png; the table above is the same data)")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
