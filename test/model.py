"""CAPM expected returns and constrained quadratic mean-variance allocation."""

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize

from .config import BacktestConfig


@dataclass(frozen=True)
class Estimates:
    beta: np.ndarray
    expected_return: np.ndarray
    covariance: np.ndarray
    market_premium: float
    risk_free: float


def estimate(asset_returns: np.ndarray, market_returns: np.ndarray, risk_free: np.ndarray, config: BacktestConfig) -> Estimates:
    """과거 수익률에서 CAPM 기대수익과 공분산을 추정한다."""
    assets = np.asarray(asset_returns, dtype=float)
    market = np.asarray(market_returns, dtype=float)
    rf = np.asarray(risk_free, dtype=float)
    if assets.ndim != 2 or len(assets) < 3 or market.shape != (len(assets),) or rf.shape != (len(assets),):
        raise ValueError("invalid estimation window shape")
    if not all(np.isfinite(a).all() for a in (assets, market, rf)):
        raise ValueError("non-finite estimation input")

    # 베타 = 자산과 시장의 초과수익 공분산 / 시장 초과수익 분산.
    market_excess = market - rf
    centered_market = market_excess - market_excess.mean()
    denominator = centered_market @ centered_market
    if denominator < 1e-16:
        raise ValueError("market excess return variance is zero")
    asset_excess = assets - rf[:, None]
    beta = (asset_excess - asset_excess.mean(axis=0)).T @ centered_market / denominator

    # CAPM: 무위험수익 + 베타 × 시장 초과수익. 일별 추정치를 연율화한다.
    expected = (rf.mean() + beta * market_excess.mean()) * config.annualization

    # 공분산의 불안정성을 줄이기 위해 일부를 대각 행렬로 축소한다.
    sample = np.atleast_2d(np.cov(assets, rowvar=False, ddof=1)) * config.annualization
    shrinkage = config.covariance_shrinkage
    covariance = (1 - shrinkage) * sample + shrinkage * np.diag(np.diag(sample))
    covariance += np.eye(assets.shape[1]) * 1e-8
    return Estimates(beta, expected, covariance, float(market_excess.mean() * config.annualization), float(rf.mean() * config.annualization))


def optimize(expected: np.ndarray, covariance: np.ndarray, risk_aversion: float, max_weight: float, initial=None,
             *, current_weights=None, cost_penalty: float = 0.0) -> np.ndarray:
    """기대수익 − 위험 벌점 − 비용 벌점을 최대화한다. 입력은 연율 기준이다."""
    expected = np.asarray(expected, dtype=float)
    covariance = np.asarray(covariance, dtype=float)
    n = len(expected)
    if n < 2 or covariance.shape != (n, n) or not np.isfinite(expected).all() or not np.isfinite(covariance).all():
        raise ValueError("invalid optimizer inputs")
    if not np.allclose(covariance, covariance.T) or np.linalg.eigvalsh(covariance).min() < -1e-10:
        raise ValueError("covariance must be symmetric positive semidefinite")
    if risk_aversion <= 0 or not 1 / n <= max_weight <= 1:
        raise ValueError("infeasible optimizer constraints")
    if not np.isfinite(cost_penalty) or cost_penalty < 0:
        raise ValueError("cost_penalty must be finite and nonnegative")
    if current_weights is None:
        if cost_penalty:
            raise ValueError("current holdings are required to estimate turnover")
    else:
        current_weights = np.asarray(current_weights, dtype=float)
        if (current_weights.shape != (n,) or not np.isfinite(current_weights).all()
                or current_weights.min() < -1e-9 or current_weights.sum() > 1 + 1e-7):
            raise ValueError("invalid current weights")
    if initial is None:
        initial = np.full(n, 1 / n)
    initial = np.asarray(initial, dtype=float)
    if initial.shape != (n,) or not np.isfinite(initial).all() or abs(initial.sum() - 1) > 1e-7 or initial.min() < -1e-7 or initial.max() > max_weight + 1e-7:
        raise ValueError("invalid optimizer warm start")

    def objective(x):
        weights = x[:n]
        portfolio_return = expected @ weights
        portfolio_variance = weights @ covariance @ weights
        utility = portfolio_return - risk_aversion / 2 * portfolio_variance
        if cost_penalty:
            utility -= cost_penalty * x[n:].sum()
        # minimize()를 쓰므로 부호를 뒤집는다. 100배는 수치 정밀도용 배율이다.
        return -100 * utility

    def gradient(x):
        gradient_weights = 100 * (risk_aversion * covariance @ x[:n] - expected)
        return np.r_[gradient_weights, np.full(n, 100 * cost_penalty)] if cost_penalty else gradient_weights

    bounds = [(0, max_weight)] * n
    equality_gradient = np.ones(n)
    constraints = []
    if cost_penalty:
        # z >= w-current, z >= current-w 이면 비용은 c*sum(z).
        # 절댓값의 꺾이는 지점도 근사하지 않고 선형 제약으로 처리한다.
        initial = np.r_[initial, np.abs(initial - current_weights)]
        bounds += [(0, None)] * n
        equality_gradient = np.r_[np.ones(n), np.zeros(n)]
        difference_matrix = np.block([[-np.eye(n), np.eye(n)], [np.eye(n), np.eye(n)]])
        offset = np.r_[current_weights, -current_weights]
        constraints.append({"type": "ineq", "fun": lambda x: difference_matrix @ x + offset,
                            "jac": lambda x: difference_matrix})
    constraints.append({"type": "eq", "fun": lambda x: x[:n].sum() - 1,
                        "jac": lambda x: equality_gradient})

    # 비중 합은 100%, 각 비중은 0~max_weight. 전일 비중에서 탐색을 시작한다.
    result = minimize(
        objective, initial, jac=gradient, method="SLSQP",
        bounds=bounds, constraints=constraints,
        options={"ftol": 1e-10, "maxiter": 200},
    )
    weights = np.asarray(result.x[:n])
    if not result.success or not np.isfinite(weights).all() or abs(weights.sum() - 1) > 1e-7 or weights.min() < -1e-7 or weights.max() > max_weight + 1e-7:
        raise RuntimeError(f"optimization failed: {result.message}")
    if cost_penalty and (difference_matrix @ result.x + offset).min() < -1e-7:
        raise RuntimeError("optimizer violated transaction cost constraints")
    if objective(result.x) > objective(initial) + 1e-7:
        raise RuntimeError("optimizer produced a worse objective than its feasible starting point")
    weights = np.clip(weights, 0, max_weight)
    return weights / weights.sum()


def decide_rebalance(estimates: Estimates, current_weights: np.ndarray, config: BacktestConfig) -> dict:
    """신호일의 실제 보유 비중을 기준으로, 거래와 그대로 보유하기를 비교한다."""
    current = np.asarray(current_weights, dtype=float)
    horizon = config.cost_horizon_days / config.annualization
    fee_rate = config.fee_bps / 10_000
    fully_invested = abs(current.sum() - 1) < 1e-7
    within_limit = current.max() <= config.max_weight + 1e-7
    initial = current if fully_invested and within_limit else None
    weights = optimize(estimates.expected_return, estimates.covariance, config.risk_aversion,
                       config.max_weight, initial, current_weights=current,
                       cost_penalty=fee_rate / horizon)

    def utility(w):
        # 현금이 남아 있으면 기대 RF를 포함한다. 위험자산 목표 비중의 합은 1이다.
        expected = estimates.expected_return @ w + (1 - w.sum()) * estimates.risk_free
        return float(horizon * (expected - config.risk_aversion / 2 * (w @ estimates.covariance @ w)))

    hold_utility, target_utility = utility(current), utility(weights)
    cost = float(fee_rate * np.abs(weights - current).sum())
    net_gain = target_utility - hold_utility - cost
    if not fully_invested:
        reason = "initial_investment"
    elif not within_limit:
        reason = "weight_limit"
    elif net_gain > 1e-10:
        reason = "net_utility_gain"
    else:
        reason = "hold"
    trade = reason != "hold"
    return {"weights": weights if trade else current.copy(), "candidate_weights": weights,
            "trade": trade, "reason": reason,
            "hold_utility": hold_utility, "candidate_utility": target_utility,
            "estimated_cost": cost, "net_utility_gain": net_gain}
