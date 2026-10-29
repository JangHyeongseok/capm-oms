"""전일 CAPM 비중 → 정수 주문 → HTTP 서버 → 가상 체결 → 계좌 확인."""

import argparse
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import numpy as np
import pandas as pd

from test.config import BacktestConfig
from test.data import load_data
from test.model import estimate, optimize


def closing_quotes(data: Path, symbols, day: pd.Timestamp) -> dict[str, Decimal]:
    """조정가격의 가상 단위 대신 원본 close로 정수 주식 주문을 만든다."""
    quotes = {}
    for symbol in symbols:
        raw = json.loads((data / "raw" / f"{symbol}.json").read_text())["chart"]["result"][0]
        dates = pd.to_datetime(raw["timestamp"], unit="s", utc=True).tz_convert("America/New_York").date
        index = list(dates).index(day.date())
        price = Decimal(str(raw["indicators"]["quote"][0]["close"][index]))
        if not price.is_finite() or price <= 0:
            raise ValueError(f"invalid quote: {symbol}")
        quotes[symbol] = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return quotes


def value_account(account: dict, quotes: dict[str, Decimal]) -> dict:
    positions = account["positions"]
    if set(positions) - quotes.keys():
        raise ValueError("account holds assets outside the configured ETF universe")
    cash = Decimal(account["cash"])
    holdings_value = sum((quotes[s] * q for s, q in positions.items()), Decimal("0.00"))
    return {"cash": str(cash), "holdings_value": str(holdings_value),
            "total_value": str(cash + holdings_value), "positions": positions}


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
    quotes = closing_quotes(data, config.symbols, day)

    with httpx.Client(base_url=url, timeout=15) as client:
        def call(method, path, body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        health = call("GET", "/health")
        if health.get("mode") != "local-simulation" or not health.get("fills_enabled"):
            raise RuntimeError("a local SimBroker server is required")
        account = call("GET", "/account")
        if Decimal(account["reserved_cash"]) or account["reserved_positions"]:
            raise RuntimeError("reconcile or cancel outstanding orders before rebalancing")
        before = value_account(account, quotes)
        nav = Decimal(before["total_value"])
        if nav <= 0:
            raise ValueError("account has no capital")

        # 2. 총자산 × 목표 비중을 정수 주식 수로 내림한다. 남는 금액은 현금이다.
        allocations, plan = [], []
        for j, symbol in enumerate(config.symbols):
            target = int(nav * Decimal(str(weights[j])) / quotes[symbol])
            change = target - account["positions"].get(symbol, 0)
            allocations.append({"symbol": symbol, "target_weight": float(weights[j]),
                                "expected_annual_return": float(estimates.expected_return[j]),
                                "annualized_volatility": float(np.sqrt(estimates.covariance[j, j])),
                                "quote": str(quotes[symbol]), "target_quantity": target})
            if change:
                plan.append({"symbol": symbol, "side": "buy" if change > 0 else "sell",
                             "quantity": abs(change), "limit_price": str(quotes[symbol])})

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
            if final["status"] != "FILLED" or final["filled_quantity"] != request["quantity"]:
                raise RuntimeError(f"fill is unconfirmed; inspect /orders/{order['order_id']}")
            completed.append({**request, "order_id": final["order_id"], "status": final["status"],
                              "filled_quantity": final["filled_quantity"],
                              "average_fill_price": final["average_fill_price"]})

        # 4. 실제 체결 기록으로 복원한 현금과 보유 수량이 목표 수량과 맞는지 확인한다.
        account = call("GET", "/account")
        after = value_account(account, quotes)
        expected = {a["symbol"]: a["target_quantity"] for a in allocations if a["target_quantity"]}
        if (account["positions"] != expected or Decimal(account["cash"]) < 0
                or Decimal(account["reserved_cash"]) or account["reserved_positions"]
                or after["total_value"] != before["total_value"]):
            raise RuntimeError("account did not reconcile; inspect orders before retrying")

    return {"mode": "offline-paper-trading", "signal_date": str(signal_day.date()),
            "quote_date": str(day.date()), "server": url,
            "execution_model": "observed close snapshot; immediate full fills; no fees or slippage",
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
        print(f"{order['symbol']:3} {order['side']:4} {order['filled_quantity']:>5} @ {order['average_fill_price']} -> {order['status']}")
    after = result["account_after"]
    print(f"Cash ${after['cash']} | ETF ${after['holdings_value']} | Total ${after['total_value']}")
    print(f"Saved: {output.resolve()}")


if __name__ == "__main__":
    main()
