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
    x = np.asarray(asset_returns, dtype=float)
    market = np.asarray(market_returns, dtype=float)
    rf = np.asarray(risk_free, dtype=float)
    if x.ndim != 2 or len(x) < 3 or market.shape != (len(x),) or rf.shape != (len(x),):
        raise ValueError("invalid estimation window shape")
    if not all(np.isfinite(a).all() for a in (x, market, rf)):
        raise ValueError("non-finite estimation input")
    market_excess = market - rf
    centered_market = market_excess - market_excess.mean()
    denominator = centered_market @ centered_market
    if denominator < 1e-16:
        raise ValueError("market excess return variance is zero")
    asset_excess = x - rf[:, None]
    beta = (asset_excess - asset_excess.mean(axis=0)).T @ centered_market / denominator
    expected = (rf.mean() + beta * market_excess.mean()) * config.annualization
    sample = np.atleast_2d(np.cov(x, rowvar=False, ddof=1)) * config.annualization
    shrinkage = config.covariance_shrinkage
    covariance = (1 - shrinkage) * sample + shrinkage * np.diag(np.diag(sample))
    covariance += np.eye(x.shape[1]) * 1e-8
    return Estimates(beta, expected, covariance, float(market_excess.mean() * config.annualization), float(rf.mean() * config.annualization))


def optimize(expected: np.ndarray, covariance: np.ndarray, risk_aversion: float, max_weight: float, initial=None) -> np.ndarray:
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

    def objective(w):
        return 100 * (risk_aversion / 2 * w @ covariance @ w - expected @ w)

    def gradient(w):
        return 100 * (risk_aversion * covariance @ w - expected)

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
