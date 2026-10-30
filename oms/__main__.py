"""python -m oms: 과거 가격으로 전략을 실행하고 주문을 체결한다."""

import argparse
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from test.backtest import allocate_after_cost, build_targets, metrics, period_positions
from test.config import BacktestConfig
from test.data import load_data
from test.model import decide_rebalance, estimate

from .domain import FillRequest, OrderRequest
from .store import Store


def decimal(value):
    return Decimal(str(value))


def valuation(units, cash, price):
    return cash + sum((Decimal(int(q)) * decimal(p) for q, p in zip(units, price)), Decimal(0))


def daily_row(day, units, cash, price, previous_nav, wealth, fees, notional, symbols):
    nav = valuation(units, cash, price)
    return {"date": day, "nav": float(nav), "net_return": float(nav / previous_nav - 1),
            "gross_return": float(wealth / previous_nav - 1), "fee": float(fees),
            "traded_notional": float(notional), "turnover": float(notional / wealth),
            "cash": float(cash), "exact_cash": str(cash), "exact_nav": str(nav),
            **{f"units_{s}": int(q) for s, q in zip(symbols, units)}}


def run_strategy(prices, risk_free, config, output, *, targets=None):
    """전일 전략 판단 → 오늘 주문·체결 → 현금·보유 수량 갱신을 반복한다."""
    if output.exists() and any(output.iterdir()):
        raise ValueError("기존 결과를 보존하도록 새 출력 폴더를 지정해 주세요.")
    output.mkdir(parents=True, exist_ok=True)
    store = Store(output / "orders.sqlite3")
    start, end = period_positions(prices.index, config)
    symbols = list(config.symbols)
    quotes = prices[symbols].to_numpy()
    returns = prices.pct_change(fill_method=None)
    units = np.zeros(len(symbols), dtype=np.int64)
    cash = previous_nav = decimal(config.initial_capital)
    fee_rate = decimal(config.fee_bps) / 10_000
    curve = [daily_row(prices.index[start - 1], units, cash, quotes[start - 1],
                       previous_nav, previous_nav, Decimal(0), Decimal(0), symbols)]
    fills = []

    for position in range(start, end + 1):
        day, signal_day = prices.index[position], prices.index[position - 1]
        # 1. 전일 종가까지의 정보로 목표 비중과 거래 여부를 정한다.
        if targets is None:
            window = slice(position - config.lookback, position)
            estimation = estimate(returns[symbols].iloc[window].to_numpy(),
                                  returns[config.market].iloc[window].to_numpy(),
                                  risk_free.iloc[window].to_numpy(), config)
            current = units * quotes[position - 1] / float(previous_nav)
            decision = decide_rebalance(estimation, current, config)
            weights, should_trade = decision["weights"], decision["trade"]
        else:
            weights, should_trade = targets.loc[signal_day].to_numpy(), True

        # 2. 현금 이자를 반영하고 오늘 종가로 기존 보유분을 평가한다.
        cash *= 1 + decimal(risk_free.iloc[position])
        price = quotes[position]
        wealth = valuation(units, cash, price)
        fees = notional = Decimal(0)
        if should_trade:
            allocated, _ = allocate_after_cost(units * price, weights, float(wealth), float(fee_rate))
            changes = np.floor(allocated / price).astype(np.int64) - units
            # 3. 매도부터 주문 → 접수 → 체결하고, 확인된 체결만 계좌에 반영한다.
            for j in sorted(range(len(symbols)), key=lambda j: int(changes[j])):
                change = int(changes[j])
                if not change:
                    continue
                order = store.create(OrderRequest(symbol=symbols[j], quantity=abs(change),
                                                  price=float(price[j]), kind="buy" if change > 0 else "sell"))
                store.accept(order["order_id"])
                order = store.fill(order["order_id"], FillRequest(quantity=order["quantity"]))
                signed = order["filled_quantity"] * (1 if order["kind"] == "buy" else -1)
                amount = Decimal(abs(signed)) * decimal(order["price"])
                fee = amount * fee_rate
                units[j] += signed
                cash += amount if signed < 0 else -amount
                cash -= fee
                fees += fee
                notional += amount
                if cash < 0 or units.min() < 0:
                    raise RuntimeError(f"현금 또는 보유 수량이 부족합니다: {day}")
                fills.append({"signal_date": signal_day, "execution_date": day,
                              **order, "notional": str(amount), "fee": str(fee)})

        # 4. 당일 평가액·현금·수량을 기록한다.
        row = daily_row(day, units, cash, price, previous_nav, wealth, fees, notional, symbols)
        curve.append(row)
        previous_nav = decimal(row["exact_nav"])

    curve, fills = pd.DataFrame(curve).set_index("date"), pd.DataFrame(fills)
    curve.to_csv(output / "oms_curve.csv", float_format="%.17g", date_format="%Y-%m-%d")
    fills.to_csv(output / "oms_orders.csv", index=False, float_format="%.17g", date_format="%Y-%m-%d")
    summary = metrics(curve, risk_free, config.annualization)
    (output / "metrics.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"{output.name}: {len(fills):,}개 체결, 최종 평가액 ${previous_nav:,.2f}", flush=True)
    return curve, fills


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/daily-capm.json"))
    parser.add_argument("--data", type=Path, default=Path("datasets"))
    parser.add_argument("--output", type=Path, default=Path("results/oms-run"))
    parser.add_argument("--strategy", choices=["both", "capm_cost_aware", "capm_mean_variance"], default="both")
    parser.add_argument("--end")
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("기존 결과를 보존하도록 새 출력 폴더를 지정해 주세요.")
    config = BacktestConfig.load(args.config)
    if args.end:
        config = replace(config, end=args.end)
    prices, risk_free, _ = load_data(args.data, config)
    strategies = ["capm_cost_aware", "capm_mean_variance"] if args.strategy == "both" else [args.strategy]
    targets = build_targets(prices, risk_free, config)[0] if "capm_mean_variance" in strategies else {}
    for strategy in strategies:
        run_strategy(prices, risk_free, config, args.output / strategy, targets=targets.get(strategy))


if __name__ == "__main__":
    main()
