"""A close-to-close, one-session delayed, self-financing research ledger."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import ResearchConfig
from .model import estimate, optimize


@dataclass
class BacktestResult:
    curve: pd.DataFrame
    trades: pd.DataFrame
    holdings: pd.DataFrame


def period_positions(index: pd.DatetimeIndex, config: ResearchConfig) -> tuple[int, int]:
    positions = np.flatnonzero((index >= config.start) & (index <= config.end))
    if len(positions) < 2 or positions[0] < config.lookback + 1:
        raise ValueError("insufficient backtest days or pre-start estimation history")
    return int(positions[0]), int(positions[-1])


def build_targets(prices: pd.DataFrame, risk_free: pd.Series, config: ResearchConfig):
    """매일 과거 구간만 잘라 기대수익 추정 → 목표 비중 계산을 반복한다."""
    start, end = period_positions(prices.index, config)
    returns = prices.pct_change(fill_method=None)
    assets = list(config.symbols)
    signal_dates = prices.index[start - 1:end]
    capm_rows, minimum_rows, diagnostics = [], [], []
    previous_capm = previous_minimum = None
    for position in range(start - 1, end):
        # 오늘 종가까지 관측한 수익률만 쓴다. 체결은 다음 거래일이다.
        window = slice(position - config.lookback + 1, position + 1)
        estimation = estimate(
            returns[assets].iloc[window].to_numpy(),
            returns[config.market].iloc[window].to_numpy(),
            risk_free.iloc[window].to_numpy(), config,
        )
        capm = optimize(estimation.expected_return, estimation.covariance, config.risk_aversion, config.max_weight, previous_capm)
        minimum = optimize(np.zeros(len(assets)), estimation.covariance, config.risk_aversion, config.max_weight, previous_minimum)
        previous_capm, previous_minimum = capm, minimum
        capm_rows.append(capm)
        minimum_rows.append(minimum)
        for j, symbol in enumerate(assets):
            diagnostics.append({
                "signal_date": prices.index[position], "execution_date": prices.index[position + 1],
                "window_start": prices.index[position - config.lookback + 1], "window_end": prices.index[position],
                "observations": config.lookback, "symbol": symbol,
                "beta": estimation.beta[j], "expected_annual_return": estimation.expected_return[j],
                "estimated_market_premium": estimation.market_premium,
                "estimated_risk_free": estimation.risk_free,
                "target_weight": capm[j],
            })
    targets = {
        "capm_mean_variance": pd.DataFrame(capm_rows, index=signal_dates, columns=assets),
        "minimum_variance": pd.DataFrame(minimum_rows, index=signal_dates, columns=assets),
        "equal_weight": pd.DataFrame(1 / len(assets), index=signal_dates, columns=assets),
        "spy_buy_hold": pd.DataFrame(1.0, index=signal_dates, columns=[config.market]),
    }
    return targets, pd.DataFrame(diagnostics)


def allocate_after_cost(current: np.ndarray, target: np.ndarray, wealth: float, fee_rate: float) -> tuple[np.ndarray, float]:
    """거래 후 투자금 + 매수·매도 비용 = 거래 전 자산이 되도록 투자금을 구한다."""
    current, target = np.asarray(current, dtype=float), np.asarray(target, dtype=float)
    if current.shape != target.shape or not np.isfinite(current).all() or not np.isfinite(target).all() or current.min() < -1e-9 or target.min() < -1e-9 or abs(target.sum() - 1) > 1e-7:
        raise ValueError("invalid holdings or target weights")
    if not np.isfinite(wealth) or wealth <= 0 or current.sum() > wealth + 1e-7 * wealth or not 0 <= fee_rate < 0.1:
        raise ValueError("invalid wealth or cost rate")
    low, high = 0.0, wealth
    for _ in range(50):
        capital = (low + high) / 2
        residual = capital + fee_rate * np.abs(target * capital - current).sum() - wealth
        if residual > 0:
            high = capital
        else:
            low = capital
    allocated = target * ((low + high) / 2)
    fee = float(fee_rate * np.abs(allocated - current).sum())
    if abs(allocated.sum() + fee - wealth) > wealth * 1e-10:
        raise RuntimeError("self-financing allocation failed")
    return allocated, fee


def simulate(prices: pd.DataFrame, risk_free: pd.Series, targets: pd.DataFrame, config: ResearchConfig, buy_and_hold: bool = False) -> BacktestResult:
    """전일 목표 비중을 오늘 종가에 적용하며 거래·비용·평가액을 기록한다."""
    start, end = period_positions(prices.index, config)
    symbols = list(targets.columns)
    units = np.zeros(len(symbols))
    cash = previous_nav = config.initial_capital
    curve = [{"date": prices.index[start - 1], "nav": previous_nav, "net_return": 0.0, "gross_return": 0.0,
              "fee": 0.0, "traded_notional": 0.0, "turnover": 0.0, "cash": cash}]
    trades, holdings = [], []
    for position in range(start, end + 1):
        execution_date, signal_date = prices.index[position], prices.index[position - 1]
        price = prices[symbols].iloc[position].to_numpy(dtype=float)
        if not np.isfinite(price).all() or (price <= 0).any() or not np.isfinite(risk_free.iloc[position]):
            raise ValueError("invalid execution price or risk-free return")
        # 1. 오늘 가격으로 기존 보유분과 현금을 평가한다.
        cash *= 1 + float(risk_free.iloc[position])
        current = units * price
        wealth = float(current.sum() + cash)
        fee, traded_notional = 0.0, 0.0
        if not buy_and_hold or position == start:
            if signal_date not in targets.index:
                raise ValueError(f"missing pre-execution signal for {execution_date}")
            # 2. 전일 신호를 읽고, 보유액과 목표액의 차이만 거래한다.
            weights = targets.loc[signal_date].to_numpy(dtype=float)
            allocated, fee = allocate_after_cost(current, weights, wealth, config.fee_bps / 10_000)
            delta = allocated - current
            traded_notional = float(np.abs(delta).sum())
            for j, symbol in enumerate(symbols):
                if abs(delta[j]) > 1e-8:
                    trades.append({
                        "signal_date": signal_date, "execution_date": execution_date, "symbol": symbol,
                        "side": "BUY" if delta[j] > 0 else "SELL", "adjusted_units": abs(delta[j] / price[j]),
                        "adjusted_price": price[j], "notional": abs(delta[j]),
                        "fee": abs(delta[j]) * config.fee_bps / 10_000,
                        "target_weight": weights[j],
                    })
            units = allocated / price
            cash = wealth - float(allocated.sum()) - fee
            if abs(cash) < wealth * 1e-12:
                cash = 0.0
        # 3. 비용 차감 후 자산을 확인하고 오늘의 수익률을 저장한다.
        values = units * price
        nav = float(values.sum() + cash)
        if nav <= 0 or abs(nav + fee - wealth) > wealth * 1e-9:
            raise RuntimeError("portfolio accounting invariant violated")
        curve.append({"date": execution_date, "nav": nav, "net_return": nav / previous_nav - 1,
                      "gross_return": wealth / previous_nav - 1, "fee": fee,
                      "traded_notional": traded_notional, "turnover": traded_notional / wealth, "cash": cash})
        holdings.append({"date": execution_date, **{s: values[j] / nav for j, s in enumerate(symbols)}})
        previous_nav = nav
    columns = ["signal_date", "execution_date", "symbol", "side", "adjusted_units", "adjusted_price", "notional", "fee", "target_weight"]
    return BacktestResult(pd.DataFrame(curve).set_index("date"), pd.DataFrame(trades, columns=columns), pd.DataFrame(holdings).set_index("date"))


def metrics(curve: pd.DataFrame, risk_free: pd.Series, annualization: int) -> dict:
    nav = curve["nav"]
    daily = curve["net_return"].iloc[1:]
    rf = risk_free.reindex(daily.index)
    excess = daily - rf
    years = len(daily) / annualization
    drawdown = nav / nav.cummax() - 1
    excess_std = float(excess.std(ddof=1))
    return {
        "start": str(daily.index[0].date()), "end": str(daily.index[-1].date()),
        "trading_days": len(daily), "initial_nav": float(nav.iloc[0]), "final_nav": float(nav.iloc[-1]),
        "total_return": float(nav.iloc[-1] / nav.iloc[0] - 1),
        "cagr": float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1),
        "annualized_volatility": float(daily.std(ddof=1) * np.sqrt(annualization)),
        "sharpe": float(excess.mean() / excess_std * np.sqrt(annualization)) if excess_std > 1e-15 else None,
        "max_drawdown": float(drawdown.min()), "total_fees": float(curve["fee"].sum()),
        "annualized_turnover": float(curve["turnover"].sum() / years),
    }
