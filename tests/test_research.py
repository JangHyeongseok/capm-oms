from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from research.backtest import allocate_after_cost, build_targets, metrics, simulate
from research.config import ResearchConfig
from research.model import estimate, optimize


def fixture_market():
    rng = np.random.default_rng(41)
    dates = pd.bdate_range("2020-01-01", periods=35)
    market = rng.normal(0.0005, 0.012, len(dates))
    a = 0.5 * market + rng.normal(0, 0.002, len(dates))
    b = 1.5 * market + rng.normal(0, 0.002, len(dates))
    prices = pd.DataFrame(np.cumprod(1 + np.array([a, b, market]).T, axis=0) * 100, index=dates, columns=["AAA", "BBB", "SPY"])
    config = ResearchConfig(symbols=("AAA", "BBB"), data_start="2019-01-01", start=str(dates[15].date()), end=str(dates[-1].date()), lookback=10, max_weight=0.8)
    return prices, pd.Series(0.0001, index=dates), config


def test_capm_beta_and_expected_return_known_case():
    config = ResearchConfig()
    market = np.array([-0.02, 0.01, 0.03, -0.01, 0.005])
    rf = np.full(len(market), 0.0001)
    assets = np.column_stack([rf + 0.5 * (market - rf), rf + 1.5 * (market - rf)])
    estimation = estimate(assets, market, rf, config)
    np.testing.assert_allclose(estimation.beta, [0.5, 1.5], atol=1e-12)
    np.testing.assert_allclose(estimation.expected_return, (rf.mean() + np.array([0.5, 1.5]) * (market - rf).mean()) * 252)
    assert np.linalg.eigvalsh(estimation.covariance).min() > 0


def test_minimum_variance_known_solution_and_cap():
    covariance = np.diag([0.04, 0.16])
    np.testing.assert_allclose(optimize(np.zeros(2), covariance, 5, 1), [0.8, 0.2], atol=1e-6)
    np.testing.assert_allclose(optimize(np.zeros(2), covariance, 5, 0.6), [0.6, 0.4], atol=1e-6)
    with pytest.raises(ValueError, match="infeasible"):
        optimize(np.zeros(2), covariance, 5, 0.4)


def test_fees_are_paid_without_borrowing():
    allocated, fee = allocate_after_cost(np.array([0.0, 0.0]), np.array([0.25, 0.75]), 100_000, 0.001)
    assert allocated.sum() + fee == pytest.approx(100_000, abs=1e-7)
    assert fee == pytest.approx(100_000 / 1.001 * 0.001)
    np.testing.assert_allclose(allocated / allocated.sum(), [0.25, 0.75])
    reallocated, fee = allocate_after_cost(allocated, np.array([0.75, 0.25]), allocated.sum(), 0.001)
    assert reallocated.sum() + fee == pytest.approx(allocated.sum())
    assert fee == pytest.approx(np.abs(reallocated - allocated).sum() * 0.001)


def test_future_prices_and_rates_cannot_change_past_signals():
    prices, rf, config = fixture_market()
    original, diagnostics = build_targets(prices, rf, config)
    changed = prices.copy()
    changed.iloc[25:] *= np.linspace(1.2, 1.8, len(changed) - 25)[:, None]
    changed_rf = rf.copy()
    changed_rf.iloc[25:] = 0.001
    perturbed, _ = build_targets(changed, changed_rf, config)
    for name in original:
        pd.testing.assert_frame_equal(original[name].loc[:prices.index[24]], perturbed[name].loc[:prices.index[24]])
    assert (diagnostics["window_end"] == diagnostics["signal_date"]).all()
    assert (diagnostics["signal_date"] < diagnostics["execution_date"]).all()
    assert (diagnostics["observations"] == 10).all()
    a = simulate(prices, rf, original["capm_mean_variance"], config)
    b = simulate(changed, changed_rf, perturbed["capm_mean_variance"], config)
    pd.testing.assert_frame_equal(a.curve.loc[:prices.index[24]], b.curve.loc[:prices.index[24]])


def test_execution_uses_previous_close_signal_and_earns_following_return():
    prices, rf, config = fixture_market()
    config = replace(config, fee_bps=0)
    targets, _ = build_targets(prices, rf, config)
    first = prices.index.get_loc(pd.Timestamp(config.start))
    # Entire first-day surge occurs before the strategy's first close execution.
    changed = prices.copy()
    changed.loc[prices.index[first]:, "AAA"] *= 10
    fixed_targets = targets["equal_weight"].copy()
    fixed_targets.loc[:, :] = [1.0, 0.0]
    result = simulate(changed, rf * 0, fixed_targets, config)
    assert result.curve["nav"].iloc[1] == pytest.approx(config.initial_capital)
    assert result.curve["net_return"].iloc[2] == pytest.approx(changed["AAA"].iloc[first + 1] / changed["AAA"].iloc[first] - 1)
    assert (result.trades["signal_date"] < result.trades["execution_date"]).all()


def test_flat_market_costs_turnover_and_holdings_reconcile():
    prices, rf, config = fixture_market()
    prices.loc[:, :] = 100.0
    target = pd.DataFrame([[0.5, 0.5]] * len(prices), index=prices.index, columns=["AAA", "BBB"])
    paid = simulate(prices, rf * 0, target, replace(config, fee_bps=10))
    free = simulate(prices, rf * 0, target, replace(config, fee_bps=0))
    assert paid.curve["nav"].iloc[-1] == pytest.approx(100_000 / 1.001)
    assert free.curve["nav"].iloc[-1] == pytest.approx(100_000)
    assert paid.trades["fee"].sum() == pytest.approx(paid.curve["fee"].sum())
    assert paid.trades["notional"].sum() == pytest.approx(paid.curve["traded_notional"].sum())
    np.testing.assert_allclose(paid.holdings.sum(axis=1), 1, atol=1e-12)
    np.testing.assert_allclose(paid.curve["cash"].iloc[1:], 0, atol=1e-7)
    assert metrics(paid.curve, rf * 0, 252)["max_drawdown"] == pytest.approx(1 / 1.001 - 1)


def test_buy_hold_never_rebalances_after_initial_purchase():
    prices, rf, config = fixture_market()
    targets, _ = build_targets(prices, rf, config)
    result = simulate(prices, rf, targets["spy_buy_hold"], config, buy_and_hold=True)
    assert len(result.trades) == 1
    assert result.trades.iloc[0]["symbol"] == "SPY"


@pytest.mark.parametrize("change", [{"max_weight": 0.01}, {"risk_aversion": 0}, {"fee_bps": -1}, {"fee_bps": float("nan")}, {"lookback": 0}, {"symbols": ("XLF", "XLF")}])
def test_invalid_config_fails(change):
    with pytest.raises(ValueError):
        ResearchConfig(**change)


def test_missing_data_fails_instead_of_forward_fill():
    prices, rf, config = fixture_market()
    prices.iloc[8, 0] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        build_targets(prices, rf, config)
