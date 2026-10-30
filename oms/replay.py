"""과거 거래일을 순서대로 OMS에서 실행하고 날짜별 계좌를 백테스트와 대조한다."""

import argparse
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import httpx
import pandas as pd

from test.backtest import build_targets, simulate
from test.config import BacktestConfig
from test.data import load_data, sha256

from .paper import closing_quotes, execute_targets


def replay(url: str, config_path: Path, data: Path, output: Path, start=None, end=None,
           strategy="capm_cost_aware") -> dict:
    started = perf_counter()
    if output.exists() and any(output.iterdir()):
        raise ValueError("output is not empty; preserve it and choose a new output directory")
    base = BacktestConfig.load(config_path)
    start = start or base.start
    end = end or str((pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date())
    if start < base.start or end > base.end:
        raise ValueError("replay dates must remain inside the configured backtest period")
    config = replace(base, start=start, end=end)
    prices, risk_free, manifest = load_data(data, config)

    # 같은 순서의 최적화 결과를 두 원장에 전달한다. 백테스트 거래 내역을 주문으로 복사하지 않는다.
    if strategy == "capm_cost_aware":
        reference = simulate(prices, risk_free, None, config, cost_aware=True)
        weights = reference.signals.pivot(index="signal_date", columns="symbol", values="target_weight").loc[:, list(config.symbols)]
        decisions = reference.decisions.set_index("execution_date")
    elif strategy == "capm_mean_variance":
        targets, _ = build_targets(prices, risk_free, config)
        weights = targets[strategy]
        reference = simulate(prices, risk_free, weights, config)
        decisions = None
    else:
        raise ValueError("unknown replay strategy")
    sessions = reference.curve.index[1:]
    daily, orders = [], []
    reference_fees = reference.curve["fee"].cumsum()

    with httpx.Client(base_url=url, timeout=15) as client:
        def call(method, path, body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        health = call("GET", "/health")
        if health.get("mode") != "local-simulation" or not health.get("fills_enabled"):
            raise ValueError("a local SimBroker server is required")
        account = call("GET", "/account")
        if (account["as_of_date"] is not None or account["positions"] or account["reserved_positions"]
                or Decimal(account["total_fees"]) or Decimal(account["total_interest"])
                or Decimal(account["reserved_cash"])
                or Decimal(account["cash"]) != Decimal(str(config.initial_capital))):
            raise ValueError("replay requires an unused account with the configured initial capital")
        output.mkdir(parents=True, exist_ok=True)

        for day in sessions:
            signal_day = prices.index[prices.index.get_loc(day) - 1]
            # 1. 백테스트처럼 오늘 거래 전에 어제까지 남은 현금에 일별 RF를 적용한다.
            call("POST", f"/simulation/sessions/{day.date()}", {"interest_rate": str(risk_free.loc[day])})
            # 2. 전일 목표 비중과 오늘 종가로 실제 HTTP 주문·체결·상태 대사를 실행한다.
            quotes = closing_quotes(prices, config.symbols, day)
            should_trade = decisions is None or bool(decisions.at[day, "trade"])
            result = execute_targets(url, config, day, signal_day,
                                     weights.loc[signal_day].to_numpy() if should_trade else None, quotes)
            after = result["account_after"]
            actual = {s: Decimal(q) for s, q in after["positions"].items()}
            expected = reference.holdings.loc[day] * reference.curve.at[day, "nav"] / prices.loc[day, list(config.symbols)]
            quantity_error = max(abs(float(actual.get(s, 0)) - expected[s]) for s in config.symbols)
            nav_error = abs(float(after["total_value"]) - reference.curve.at[day, "nav"])
            cash_error = abs(float(after["cash"]) - reference.curve.at[day, "cash"])
            fee_error = abs(float(after["total_fees"]) - reference_fees.loc[day])
            passed = nav_error <= 0.01 and cash_error <= 0.01 and fee_error <= 0.01 and quantity_error <= 0.00001

            # 3. 각 거래일의 결과를 바로 저장한다. 불일치가 있으면 그 날짜에서 멈춘다.
            daily.append({"date": str(day.date()), "signal_date": str(signal_day.date()),
                          "nav": float(after["total_value"]), "reference_nav": reference.curve.at[day, "nav"],
                          "cash": float(after["cash"]), "reference_cash": reference.curve.at[day, "cash"],
                          "cumulative_fees": float(after["total_fees"]), "reference_fees": reference_fees.loc[day],
                          "nav_error_usd": nav_error, "cash_error_usd": cash_error,
                          "cumulative_fee_error_usd": fee_error, "maximum_quantity_error": quantity_error,
                          "orders": len(result["orders"]), "passed": passed})
            orders.extend({"execution_date": str(day.date()), "signal_date": str(signal_day.date()), **o}
                          for o in result["orders"])
            pd.DataFrame(daily).to_csv(output / "daily.csv", index=False, float_format="%.12g")
            pd.DataFrame(orders).to_csv(output / "orders.csv", index=False)
            print(f"{day.date()}: {len(result['orders'])} filled | NAV ${float(after['total_value']):.2f} | error ${nav_error:.8f}", flush=True)
            if not passed:
                raise RuntimeError(f"ledger mismatch on {day.date()}; inspect {output / 'daily.csv'}")

        final_account = call("GET", "/account")
    result = {"status": "passed", "strategy": strategy, "start": daily[0]["date"], "end": daily[-1]["date"],
              "trading_days": len(daily), "orders_filled": len(orders),
              "buy_orders": sum(o["side"] == "buy" for o in orders),
              "sell_orders": sum(o["side"] == "sell" for o in orders),
              "final_nav_usd": daily[-1]["nav"], "total_fees_usd": daily[-1]["cumulative_fees"],
              "maximum_nav_error_usd": max(row["nav_error_usd"] for row in daily),
              "maximum_cash_error_usd": max(row["cash_error_usd"] for row in daily),
              "maximum_cumulative_fee_error_usd": max(row["cumulative_fee_error_usd"] for row in daily),
              "maximum_quantity_error": max(row["maximum_quantity_error"] for row in daily),
              "elapsed_seconds": perf_counter() - started, "account": final_account,
              "config": config.to_dict(), "data_retrieved_at_utc": manifest["retrieved_at_utc"],
              "data_manifest_sha256": sha256(data / "manifest.json"),
              "tolerances": {"nav_cash_and_cumulative_fees_usd": 0.01, "quantity_units": 0.00001},
              "execution_model": "prior-close signal; next-close adjusted units; 8 decimal precision; 5 bps fees; daily cash RF"}
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8002")
    parser.add_argument("--config", type=Path, default=Path("configs/daily-capm.json"))
    parser.add_argument("--data", type=Path, default=Path("datasets"))
    parser.add_argument("--output", type=Path, default=Path("results/replay"))
    parser.add_argument("--start", help="시작일. 기본값은 설정의 start")
    parser.add_argument("--end", help="종료일. 기본값은 시작일이 속한 달의 말일")
    parser.add_argument("--strategy", choices=("capm_cost_aware", "capm_mean_variance"), default="capm_cost_aware")
    args = parser.parse_args()
    result = replay(args.url, args.config, args.data, args.output, args.start, args.end, args.strategy)
    print(f"Passed: {result['trading_days']} sessions, {result['orders_filled']} fills, {result['elapsed_seconds']:.2f} seconds")
    print(f"Saved: {args.output.resolve()}")


if __name__ == "__main__":
    main()
