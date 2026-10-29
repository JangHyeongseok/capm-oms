"""전일 CAPM 비중 → 수수료 차감 후 소수 수량 주문 → 가상 체결 → 계좌 확인."""

import argparse
import json
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import numpy as np
import pandas as pd

from test.backtest import allocate_after_cost
from test.config import BacktestConfig
from test.data import load_data
from test.model import estimate, optimize

from .domain import QUANTITY_STEP


def closing_quotes(prices: pd.DataFrame, symbols, day: pd.Timestamp) -> dict[str, Decimal]:
    """백테스트와 같은 조정 종가. OMS의 가격 정밀도인 소수 8자리로 저장한다."""
    return {s: Decimal(str(prices.at[day, s])).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
            for s in symbols}


def value_account(account: dict, quotes: dict[str, Decimal]) -> dict:
    positions = {s: Decimal(q) for s, q in account["positions"].items()}
    if set(positions) - quotes.keys():
        raise ValueError("account holds assets outside the configured ETF universe")
    cash = Decimal(account["cash"])
    holdings_value = sum((quotes[s] * q for s, q in positions.items()), Decimal("0.00"))
    return {"cash": str(cash), "holdings_value": str(holdings_value),
            "total_value": str(cash + holdings_value), "total_fees": account["total_fees"],
            "positions": {s: str(q) for s, q in positions.items()}}


def rebalance(url: str, config_path: Path, data: Path, date: str | None = None) -> dict:
    # 1. 전 거래일까지의 252일 수익률로만 비중을 계산한다.
    config = BacktestConfig.load(config_path)
    prices, risk_free, _ = load_data(data, config)
    sessions = prices.loc[config.start:config.end].index
    day = pd.Timestamp(date) if date else sessions[-1]
    if day not in sessions:
        raise ValueError("date must be a trading day within the configured period")
    signal_day = prices.index[prices.index.get_loc(day) - 1]
    window = prices.pct_change(fill_method=None).loc[:signal_day].tail(config.lookback)
    if len(window) != config.lookback or window.isna().any().any():
        raise ValueError("not enough history for the signal")
    estimates = estimate(window[list(config.symbols)].to_numpy(), window[config.market].to_numpy(),
                         risk_free.loc[window.index].to_numpy(), config)
    weights = optimize(estimates.expected_return, estimates.covariance, config.risk_aversion, config.max_weight)
    quotes = closing_quotes(prices, config.symbols, day)

    with httpx.Client(base_url=url, timeout=15) as client:
        def call(method, path, body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        health = call("GET", "/health")
        if health.get("mode") != "local-simulation" or not health.get("fills_enabled"):
            raise RuntimeError("a local SimBroker server is required")
        account = call("GET", "/account")
        if Decimal(account["fee_bps"]) != Decimal(str(config.fee_bps)):
            raise ValueError("OMS and backtest fee_bps must match")
        if Decimal(account["reserved_cash"]) or account["reserved_positions"]:
            raise RuntimeError("reconcile or cancel outstanding orders before rebalancing")
        before = value_account(account, quotes)
        nav = Decimal(before["total_value"])
        if nav <= 0:
            raise ValueError("account has no capital")

        # 2. 백테스트와 같은 함수로 수수료 차감 후 투자금을 구하고, 수량을 소수 8자리로 내림한다.
        positions = {s: Decimal(q) for s, q in account["positions"].items()}
        current = np.array([float(positions.get(s, 0) * quotes[s]) for s in config.symbols])
        allocated, _ = allocate_after_cost(current, weights, float(nav), config.fee_bps / 10_000)
        allocations, plan = [], []
        for j, symbol in enumerate(config.symbols):
            target = (Decimal(str(allocated[j])) / quotes[symbol]).quantize(QUANTITY_STEP, rounding=ROUND_DOWN)
            change = target - positions.get(symbol, Decimal("0"))
            allocations.append({"symbol": symbol, "target_weight": float(weights[j]),
                                "expected_annual_return": float(estimates.expected_return[j]),
                                "annualized_volatility": float(np.sqrt(estimates.covariance[j, j])),
                                "quote": str(quotes[symbol]), "target_quantity": str(target)})
            if change:
                plan.append({"symbol": symbol, "side": "buy" if change > 0 else "sell",
                             "quantity": str(abs(change)), "limit_price": str(quotes[symbol])})

        # 3. 매도부터 처리해 현금을 확보한다. 모든 주문은 실제 로컬 HTTP API를 통과한다.
        completed, run_id = [], uuid4().hex[:12]
        for request in sorted(plan, key=lambda row: row["side"] == "buy"):
            request["client_order_id"] = f"paper-{day:%Y%m%d}-{run_id}-{request['symbol']}"
            order = call("POST", "/orders", request)
            if order["status"] != "NEW":
                raise RuntimeError(f"order not accepted; inspect /orders/{order['order_id']}: {order}")

            # 이미 관측한 종가를 고정 시세로 삼는 즉시 체결 시연이다. 시장 유동성은 재현하지 않는다.
            call("POST", f"/simulation/broker-orders/{request['client_order_id']}/fills", {
                "execution_id": request["client_order_id"] + "-fill",
                "quantity": request["quantity"], "fill_price": request["limit_price"],
            })
            final = call("POST", f"/orders/{order['order_id']}/reconcile")
            if final["status"] != "FILLED" or Decimal(final["filled_quantity"]) != Decimal(request["quantity"]):
                raise RuntimeError(f"fill is unconfirmed; inspect /orders/{order['order_id']}")
            completed.append({**request, "order_id": final["order_id"], "status": final["status"],
                              "filled_quantity": final["filled_quantity"],
                              "average_fill_price": final["average_fill_price"], "total_fees": final["total_fees"]})

        # 4. 실제 체결 기록으로 복원한 현금과 보유 수량이 목표 수량과 맞는지 확인한다.
        account = call("GET", "/account")
        after = value_account(account, quotes)
        expected = {a["symbol"]: Decimal(a["target_quantity"]) for a in allocations if Decimal(a["target_quantity"])}
        actual = {s: Decimal(q) for s, q in account["positions"].items()}
        fees = Decimal(after["total_fees"]) - Decimal(before["total_fees"])
        if (actual != expected or Decimal(account["cash"]) < 0
                or Decimal(account["reserved_cash"]) or account["reserved_positions"]
                or abs(Decimal(after["total_value"]) + fees - Decimal(before["total_value"])) > Decimal("0.00000001")):
            raise RuntimeError("account did not reconcile; inspect orders before retrying")

    return {"mode": "offline-paper-trading", "signal_date": str(signal_day.date()),
            "quote_date": str(day.date()), "server": url,
            "execution_model": "adjusted close snapshot; fractional units; 5 bps fees each side; no slippage",
            "account_before": before, "allocations": allocations,
            "orders": completed, "account_after": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--config", type=Path, default=Path("configs/daily-capm.json"))
    parser.add_argument("--data", type=Path, default=Path("datasets"))
    parser.add_argument("--date", help="동봉 데이터의 거래일. 기본값은 마지막 거래일")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = rebalance(args.url, args.config, args.data, args.date)
    output = args.output or Path("results/paper") / f"{result['quote_date']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(f"Signal: {result['signal_date']} | Quote snapshot: {result['quote_date']}")
    if not result["orders"]:
        print("No orders: holdings already match target quantities.")
    for order in result["orders"]:
        print(f"{order['symbol']:3} {order['side']:4} {order['filled_quantity']:>14} @ {order['average_fill_price']} -> {order['status']} (fee ${Decimal(order['total_fees']):.6f})")
    after = result["account_after"]
    print(f"Cash ${Decimal(after['cash']):.8f} | ETF ${Decimal(after['holdings_value']):.2f} | Total ${Decimal(after['total_value']):.2f} | Fees ${Decimal(after['total_fees']):.6f}")
    print(f"Saved: {output.resolve()}")


if __name__ == "__main__":
    main()
