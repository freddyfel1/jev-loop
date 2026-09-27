"""Moving-average crossover backtest on one stock and one crypto pair.

Strategy: long when the fast SMA is above the slow SMA, flat otherwise.
Signals are computed on the close and filled at the NEXT bar's open, so the
backtest never trades on information it didn't have yet. A fee is charged
on every buy and every sell. No shorting, no leverage, no live trading.

Data: tries Yahoo Finance via yfinance first (works on a normal home
connection; on this PC it uses jev-loop's data/ca-bundle.pem automatically,
because Norton re-signs HTTPS). If that fails, it falls back to free offline
datasets:
  - GOOG daily 2004-2013, bundled with the `backtesting` pip package
  - BTC/USD hourly 2011-2017 (Bitstamp), from github.com/bukosabino/ta,
    resampled to daily bars

Usage:
  pip install pandas numpy backtesting yfinance
  python sma_backtest.py                   # SPY + BTC-USD from Yahoo, else offline
  python sma_backtest.py --offline         # force the offline datasets
  python sma_backtest.py --fast 50 --slow 200 --fee 0.001
"""

import argparse
import io
import os
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

BTC_OFFLINE_URL = "https://raw.githubusercontent.com/bukosabino/ta/master/test/data/datas.csv"

# certifi + the Norton Web/Mail Shield root, kept by the jev-loop launcher.
# Norton re-signs HTTPS on this PC, and yfinance's curl only trusts its own
# CA list, so without this every Yahoo download fails with an SSL error.
CA_BUNDLE = Path(__file__).resolve().parent.parent / "data" / "ca-bundle.pem"


def use_local_ca_bundle():
    """Point curl (yfinance) at data/ca-bundle.pem if it exists and nothing
    else was set. Returns the bundle path used, or None."""
    if os.environ.get("CURL_CA_BUNDLE") or not CA_BUNDLE.exists():
        return None
    os.environ["CURL_CA_BUNDLE"] = str(CA_BUNDLE)
    return CA_BUNDLE


def load_yahoo(ticker, start="2018-01-01"):
    import yfinance as yf

    df = yf.download(ticker, start=start, auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError(f"no data for {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close"]].dropna()


def load_offline_stock():
    from backtesting.test import GOOG

    return "GOOG", GOOG[["Open", "High", "Low", "Close"]]


def load_offline_crypto():
    raw = urllib.request.urlopen(BTC_OFFLINE_URL, timeout=30).read()
    df = pd.read_csv(io.BytesIO(raw))
    df.index = pd.to_datetime(df["Timestamp"], unit="s")
    # The raw file has sentinel rows (1.7e308) and some bad prints; drop hourly
    # bars with non-positive prices or an open far from the close.
    ohlc = df[["Open", "High", "Low", "Close"]]
    ok = (ohlc > 0).all(axis=1) & (ohlc < 1e6).all(axis=1)
    ok &= (df["Open"] / df["Close"]).between(0.5, 2)
    df = df[ok]
    daily = df.resample("1D").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    )
    return "BTC/USD", daily.dropna()


def backtest(df, fast, slow, fee, periods_per_year):
    px = df.copy()
    px["fast"] = px["Close"].rolling(fast).mean()
    px["slow"] = px["Close"].rolling(slow).mean()
    signal = (px["fast"] > px["slow"]).astype(int)
    signal[px["slow"].isna()] = 0
    # Decide at today's close, hold from tomorrow's open.
    pos = signal.shift(1).fillna(0).astype(int)

    # Return of each bar, measured open-to-open, for the position held that bar.
    open_ret = px["Open"].shift(-1) / px["Open"] - 1
    strat_ret = (pos * open_ret).fillna(0)
    trades = pos.diff().fillna(pos).abs()  # 1 on every entry and every exit
    strat_ret -= trades * fee
    equity = (1 + strat_ret).cumprod()

    # Round trips: pair each entry with its exit (or the last bar if still open).
    entries = px.index[pos.diff().fillna(pos) == 1]
    exits = px.index[pos.diff().fillna(0) == -1]
    trade_rets = []
    for i, e in enumerate(entries):
        x = exits[i] if i < len(exits) else px.index[-1]
        gross = px.at[x, "Open"] / px.at[e, "Open"]
        trade_rets.append(gross * (1 - fee) ** 2 - 1)

    years = (px.index[-1] - px.index[0]).days / 365.25
    total = equity.iloc[-1] - 1
    dd = equity / equity.cummax() - 1
    bh = px["Open"].iloc[-1] / px["Open"].iloc[0]
    bh_eq = px["Open"] / px["Open"].iloc[0]
    vol = strat_ret.std() * np.sqrt(periods_per_year)
    return {
        "period": f"{px.index[0]:%Y-%m-%d} to {px.index[-1]:%Y-%m-%d}",
        "bars": len(px),
        "total_return": total,
        "cagr": equity.iloc[-1] ** (1 / years) - 1,
        "max_drawdown": dd.min(),
        "sharpe": strat_ret.mean() * periods_per_year / vol if vol else float("nan"),
        "round_trips": len(trade_rets),
        "win_rate": np.mean([r > 0 for r in trade_rets]) if trade_rets else float("nan"),
        "time_in_market": pos.mean(),
        "buy_hold_return": bh - 1,
        "buy_hold_max_drawdown": (bh_eq / bh_eq.cummax() - 1).min(),
        "equity": equity,
    }


def fmt(r):
    pct = lambda v: f"{v * 100:,.1f}%"
    return [
        ("Period", r["period"]),
        ("Total return", pct(r["total_return"])),
        ("CAGR", pct(r["cagr"])),
        ("Max drawdown", pct(r["max_drawdown"])),
        ("Sharpe (rf=0)", f"{r['sharpe']:.2f}"),
        ("Round-trip trades", str(r["round_trips"])),
        ("Win rate", pct(r["win_rate"])),
        ("Time in market", pct(r["time_in_market"])),
        ("Buy & hold return", pct(r["buy_hold_return"])),
        ("Buy & hold max DD", pct(r["buy_hold_max_drawdown"])),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", default="SPY")
    ap.add_argument("--crypto", default="BTC-USD")
    ap.add_argument("--fast", type=int, default=20)
    ap.add_argument("--slow", type=int, default=50)
    ap.add_argument("--fee", type=float, default=0.001, help="per side, 0.001 = 0.1%%")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--csv-out", default=None, help="write equity curves to this CSV")
    a = ap.parse_args()

    datasets = []
    if not a.offline:
        use_local_ca_bundle()
        try:
            datasets = [
                (a.stock, load_yahoo(a.stock), 252),
                (a.crypto, load_yahoo(a.crypto), 365),
            ]
            source = "Yahoo Finance"
        except Exception as e:
            print(f"Yahoo unavailable ({type(e).__name__}); using offline datasets.\n")
    if not datasets:
        (sn, sd), (cn, cd) = load_offline_stock(), load_offline_crypto()
        datasets = [(sn, sd, 252), (cn, cd, 365)]
        source = "offline (backtesting.py GOOG, Bitstamp BTC via bukosabino/ta)"

    print(f"SMA {a.fast}/{a.slow} crossover, long/flat, fee {a.fee * 100:.2f}% per side")
    print(f"Data: {source}\n")
    curves = {}
    for name, df, ppy in datasets:
        r = backtest(df, a.fast, a.slow, a.fee, ppy)
        curves[name] = r["equity"]
        print(f"== {name} ==")
        for k, v in fmt(r):
            print(f"  {k:<20}{v}")
        print()
    if a.csv_out:
        pd.DataFrame(curves).to_csv(a.csv_out)


if __name__ == "__main__":
    main()
