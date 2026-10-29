"""CAPM expected returns and constrained quadratic mean-variance allocation."""

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize

from .config import ResearchConfig


@dataclass(frozen=True)
class Estimates:
    beta: np.ndarray
    expected_return: np.ndarray
    covariance: np.ndarray
    market_premium: float
    risk_free: float


def estimate(asset_returns: np.ndarray, market_returns: np.ndarray, risk_free: np.ndarray, config: ResearchConfig) -> Estimates:
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


def optimize(expected: np.ndarray, covariance: np.ndarray, risk_aversion: float, max_weight: float, initial=None) -> np.ndarray:
    """기대수익 − 위험 벌점이 최대가 되는 ETF 비중을 구한다."""
    expected = np.asarray(expected, dtype=float)
    covariance = np.asarray(covariance, dtype=float)
    n = len(expected)
    if n < 2 or covariance.shape != (n, n) or not np.isfinite(expected).all() or not np.isfinite(covariance).all():
        raise ValueError("invalid optimizer inputs")
    if not np.allclose(covariance, covariance.T) or np.linalg.eigvalsh(covariance).min() < -1e-10:
        raise ValueError("covariance must be symmetric positive semidefinite")
    if risk_aversion <= 0 or not 1 / n <= max_weight <= 1:
        raise ValueError("infeasible optimizer constraints")
    if initial is None:
        initial = np.full(n, 1 / n)
    initial = np.asarray(initial, dtype=float)
    if initial.shape != (n,) or not np.isfinite(initial).all() or abs(initial.sum() - 1) > 1e-7 or initial.min() < -1e-7 or initial.max() > max_weight + 1e-7:
        raise ValueError("invalid optimizer warm start")

    def objective(weights):
        portfolio_return = expected @ weights
        portfolio_variance = weights @ covariance @ weights
        utility = portfolio_return - risk_aversion / 2 * portfolio_variance
        # minimize()를 쓰므로 부호를 뒤집는다. 100배는 수치 정밀도용 배율이다.
        return -100 * utility

    def gradient(w):
        return 100 * (risk_aversion * covariance @ w - expected)

    # 비중 합은 100%, 각 비중은 0~max_weight. 전일 비중에서 탐색을 시작한다.
    result = minimize(
        objective, initial, jac=gradient, method="SLSQP",
        bounds=[(0, max_weight)] * n,
        constraints={"type": "eq", "fun": lambda w: w.sum() - 1, "jac": lambda w: np.ones(n)},
        options={"ftol": 1e-10, "maxiter": 200},
    )
    weights = np.asarray(result.x)
    if not result.success or not np.isfinite(weights).all() or abs(weights.sum() - 1) > 1e-7 or weights.min() < -1e-7 or weights.max() > max_weight + 1e-7:
        raise RuntimeError(f"optimization failed: {result.message}")
    if objective(weights) > objective(initial) + 1e-7:
        raise RuntimeError("optimizer produced a worse objective than its feasible starting point")
    weights = np.clip(weights, 0, max_weight)
    return weights / weights.sum()
