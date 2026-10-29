"""거래 CSV를 다시 계산해 일별 자산·비용이 맞는지 독립적으로 검사한다."""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .config import BacktestConfig
from .data import load_data, sha256


def audit(data_path: Path, output: Path) -> dict:
    run = json.loads((output / "run.json").read_text())
    values = run["config"]
    values["symbols"] = tuple(values["symbols"])
    config = BacktestConfig(**values)
    if sha256(data_path / "manifest.json") != run["data_manifest_sha256"]:
        raise ValueError("data manifest differs from the reported experiment")
    prices, rf, _ = load_data(data_path, config)
    checksums = json.loads((output / "checksums.json").read_text())
    for name, expected in checksums.items():
        if sha256(output / name) != expected:
            raise ValueError(f"modified result file: {name}")
    source_root = Path(__file__).resolve().parents[1]
    for name, expected in run["source_sha256"].items():
        if sha256(source_root / name) != expected:
            raise ValueError(f"backtest source changed since execution: {name}")
    summary = json.loads((output / "metrics.json").read_text())
    results = {}
    for strategy, recorded in summary.items():
        curve = pd.read_csv(output / f"{strategy}_curve.csv", index_col="date", parse_dates=True)
        weights = pd.read_csv(output / f"{strategy}_weights.csv", index_col="date", parse_dates=True)
        trades = pd.read_csv(output / f"{strategy}_trades.csv", parse_dates=["signal_date", "execution_date"])
        symbols = list(weights.columns)
        units, cash = np.zeros(len(symbols)), config.initial_capital
        max_error = 0.0
        groups = dict(tuple(trades.groupby("execution_date")))
        for day in curve.index[1:]:
            price = prices.loc[day, symbols].to_numpy()
            cash *= 1 + float(rf.loc[day])
            expected_signal = prices.index[prices.index.get_loc(day) - 1]
            fees = notional = 0.0
            if day in groups:
                for row in groups[day].itertuples(index=False):
                    if row.signal_date != expected_signal or row.side not in ("BUY", "SELL"):
                        raise ValueError("invalid exported trade timing or side")
                    j = symbols.index(row.symbol)
                    sign = 1 if row.side == "BUY" else -1
                    if not np.isclose(row.adjusted_price, price[j], rtol=1e-10):
                        raise ValueError("trade price differs from frozen adjusted close")
                    if not np.isclose(row.notional, row.adjusted_units * price[j], rtol=1e-9, atol=1e-7):
                        raise ValueError("trade units do not reconcile to notional")
                    if not np.isclose(row.fee, row.notional * config.fee_bps / 10_000, rtol=1e-9, atol=1e-8):
                        raise ValueError("exported transaction fee is incorrect")
                    units[j] += sign * row.adjusted_units
                    cash -= sign * row.notional + row.fee
                    fees += row.fee
                    notional += row.notional
            nav = float(units @ price + cash)
            error = abs(nav - curve.loc[day, "nav"])
            max_error = max(max_error, error)
            if error > nav * 1e-8 or cash < -nav * 1e-8 or units.min() < -1e-7:
                raise ValueError(f"ledger does not reconcile for {strategy} on {day}")
            if not np.isclose(fees, curve.loc[day, "fee"], atol=1e-5) or not np.isclose(notional, curve.loc[day, "traded_notional"], atol=1e-5):
                raise ValueError("daily trade totals do not match curve")
            if not np.allclose(units * price / nav, weights.loc[day].to_numpy(), atol=1e-8):
                raise ValueError("holdings do not match exported weights")
        if not np.isclose(nav, recorded["final_nav"], rtol=1e-8):
            raise ValueError("final NAV differs from reported metric")
        results[strategy] = {"daily_nav_checked": len(curve) - 1, "trades_replayed": len(trades), "maximum_nav_error_usd": max_error}
    return {"status": "passed", "checks": ["snapshot hashes", "result hashes", "backtest source hashes", "signal/execution timing", "fees", "cash and holdings", "every daily NAV"], "strategies": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("datasets"))
    parser.add_argument("--results", type=Path, default=Path("results/backtest"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.data, args.results)
    serialized = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized)
    print(serialized)


if __name__ == "__main__":
    main()
