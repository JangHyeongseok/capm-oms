"""Compare OMS execution with fractional and integer mathematical ledgers."""

import argparse
import importlib.metadata
import json
import platform
import sqlite3
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from oms.__main__ import daily_row, decimal, run_strategy, valuation

from .backtest import allocate_after_cost, build_targets, metrics, period_positions, simulate
from .config import BacktestConfig
from .data import load_data, sha256
from .model import decide_rebalance, estimate


def integer_plan(prices, rf, config, targets, cost_aware):
    """Calculate integer orders and balances without importing or calling Store."""
    start, end = period_positions(prices.index, config)
    symbols = list(config.symbols)
    quotes = prices[symbols].to_numpy()
    returns = prices.pct_change(fill_method=None)
    units = np.zeros(len(symbols), dtype=np.int64)
    cash = previous_nav = decimal(config.initial_capital)
    fee_rate = decimal(config.fee_bps) / 10_000
    curve = [daily_row(prices.index[start - 1], units, cash, quotes[start - 1],
                       previous_nav, previous_nav, Decimal(0), Decimal(0), symbols)]
    orders, decisions = [], []
    for position in range(start, end + 1):
        day, signal_day = prices.index[position], prices.index[position - 1]
        if cost_aware:
            window = slice(position - config.lookback, position)
            estimation = estimate(returns[symbols].iloc[window].to_numpy(),
                                  returns[config.market].iloc[window].to_numpy(),
                                  rf.iloc[window].to_numpy(), config)
            current = units * quotes[position - 1] / float(previous_nav)
            decision = decide_rebalance(estimation, current, config)
            weights, should_trade, reason = decision['weights'], decision['trade'], decision['reason']
        else:
            weights, should_trade, reason = targets.loc[signal_day].to_numpy(), True, 'daily'
        # Same idle-cash RF convention as test.backtest; no investable RF sleeve.
        cash *= 1 + decimal(rf.iloc[position])
        price = quotes[position]
        wealth = valuation(units, cash, price)
        fees = notional = Decimal(0)
        if should_trade:
            allocated, _ = allocate_after_cost(units * price, weights, float(wealth), float(fee_rate))
            new_units = np.floor(allocated / price).astype(np.int64)
            changes = new_units - units
            signed_amounts = [Decimal(int(q)) * decimal(p) for q, p in zip(changes, price)]
            notional = sum(map(abs, signed_amounts), Decimal(0))
            fees = notional * fee_rate
            cash -= sum(signed_amounts, Decimal(0)) + fees
            # Sell before buy, so the account never needs margin during rebalancing.
            for j in sorted(range(len(symbols)), key=lambda j: int(changes[j])):
                change = int(changes[j])
                if change:
                    orders.append({"signal_date": signal_day, "execution_date": day,
                                   "symbol": symbols[j], "kind": 'buy' if change > 0 else 'sell',
                                   "quantity": abs(change), "price": float(price[j])})
            units = new_units
        if cash < 0 or units.min() < 0:
            raise AssertionError(f'Integer allocation borrowed cash or sold short on {day}')
        row = daily_row(day, units, cash, price, previous_nav, wealth, fees, notional, symbols)
        curve.append(row)
        decisions.append({"signal_date": signal_day, "execution_date": day,
                          "trade_decision": should_trade, "reason": reason,
                          "has_orders": notional > 0})
        previous_nav = decimal(row['exact_nav'])
    columns = ['signal_date', 'execution_date', 'symbol', 'kind', 'quantity', 'price']
    return (pd.DataFrame(curve).set_index('date'), pd.DataFrame(orders, columns=columns),
            pd.DataFrame(decisions))


def audit_database(prices, rf, config, expected, fills, db_path):
    """Reopen persisted orders and independently reconstruct every day's account."""
    with sqlite3.connect(f'file:{db_path.resolve()}?mode=ro', uri=True) as db:
        orders = pd.read_sql_query('SELECT * FROM orders ORDER BY order_id', db)
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert len(orders) == len(fills) and orders.order_id.is_unique
    assert set(orders.order_id) == set(fills.order_id)
    for col in orders.columns.drop('order_id'):
        left = orders.set_index('order_id')[col]
        right = fills.set_index('order_id')[col]
        if col == 'time':
            left, right = pd.to_datetime(left, utc=True), pd.to_datetime(right, utc=True)
        assert left.equals(right), f'Persisted OMS field mismatch: {col}'
    mapping = fills[['order_id', 'signal_date', 'execution_date']]
    groups = dict(tuple(orders.merge(mapping, on='order_id', validate='one_to_one').groupby('execution_date')))
    symbols = list(config.symbols)
    units = {s: 0 for s in symbols}
    cash = decimal(config.initial_capital)
    fee_rate = decimal(config.fee_bps) / 10_000
    errors = {'nav_usd': 0.0, 'cash_usd': 0.0, 'daily_fee_usd': 0.0, 'quantity_units': 0}
    for day in expected.index[1:]:
        cash *= 1 + decimal(rf.loc[day])
        daily_fee = Decimal(0)
        if day in groups:
            for row in groups[day].itertuples(index=False):
                assert row.signal_date == prices.index[prices.index.get_loc(day) - 1]
                assert row.kind in ('buy', 'sell') and row.status == 'completed'
                assert row.quantity == row.filled_quantity > 0
                assert row.price == float(prices.loc[day, row.symbol])
                sign = 1 if row.kind == 'buy' else -1
                amount = Decimal(row.filled_quantity) * decimal(row.price)
                fee = amount * fee_rate
                units[row.symbol] += sign * row.filled_quantity
                cash -= sign * amount + fee
                daily_fee += fee
                assert cash >= 0 and min(units.values()) >= 0
        nav = cash + sum((Decimal(units[s]) * decimal(prices.loc[day, s]) for s in symbols), Decimal(0))
        errors['nav_usd'] = max(errors['nav_usd'], float(abs(nav - decimal(expected.loc[day, 'exact_nav']))))
        errors['cash_usd'] = max(errors['cash_usd'], float(abs(cash - decimal(expected.loc[day, 'exact_cash']))))
        errors['daily_fee_usd'] = max(errors['daily_fee_usd'], abs(float(daily_fee) - expected.loc[day, 'fee']))
        errors['quantity_units'] = max(errors['quantity_units'], max(abs(units[s] - int(expected.loc[day, f'units_{s}'])) for s in symbols))
    assert max(errors.values()) < 1e-8, errors
    return {'status': 'passed', 'daily_balances_checked': len(expected) - 1,
            'persisted_orders_checked': len(orders), 'maximum_errors': errors,
            'sqlite_integrity': 'ok', 'no_short_positions_or_borrowing': True}


def write_frame(path, frame, index=True):
    frame.to_csv(path, index=index, float_format='%.17g', date_format='%Y-%m-%d')


def run(config_path, data_path, output, strategies, end=None):
    started = time.monotonic()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use a new output directory; existing replay databases are preserved.')
    config = BacktestConfig.load(config_path)
    if end:
        config = replace(config, end=end)
    assert config.risk_aversion == 5, 'This replay fixes lambda at 5.'
    prices, rf, manifest = load_data(data_path, config)
    output.mkdir(parents=True, exist_ok=True)
    print(f'Validated snapshot; lambda={config.risk_aversion}, ETF target sum=100%, no RF allocation.', flush=True)
    targets = None
    if 'capm_mean_variance' in strategies:
        print('Recalculating daily mathematical target weights...', flush=True)
        targets, _ = build_targets(prices, rf, config)
    summary = {}
    for strategy in strategies:
        print(f'{strategy}: recalculating fractional reference and independent integer ledger...', flush=True)
        cost_aware = strategy == 'capm_cost_aware'
        weights = None if cost_aware else targets[strategy]
        reference = simulate(prices, rf, weights, config, cost_aware=cost_aware)
        expected, plan, decisions = integer_plan(prices, rf, config, weights, cost_aware)
        directory = output / strategy
        directory.mkdir()
        db_path = directory / 'orders.sqlite3'
        print(f'{strategy}: executing and checking the OMS strategy loop...', flush=True)
        actual, fills = run_strategy(prices, rf, config, directory, targets=weights)
        audit = audit_database(prices, rf, config, expected, fills, db_path)
        comparison = pd.DataFrame({'fractional_nav': reference.curve.nav, 'integer_math_nav': expected.nav,
                                   'oms_nav': actual.nav, 'oms_minus_fractional_usd': actual.nav - reference.curve.nav,
                                   'oms_minus_integer_math_usd': actual.nav - expected.nav,
                                   'fractional_cash': reference.curve.cash, 'oms_cash': actual.cash})
        nav = actual.nav
        fractional_metrics = metrics(reference.curve, rf, config.annualization)
        oms_metrics = metrics(actual, rf, config.annualization)
        summary[strategy] = {
            'fractional_math': fractional_metrics, 'integer_math': metrics(expected, rf, config.annualization),
            'oms': oms_metrics, 'audit': audit, 'orders_filled': len(fills),
            'buy_orders': int((fills.kind == 'buy').sum()), 'sell_orders': int((fills.kind == 'sell').sum()),
            'fractional_trade_count': len(reference.trades),
            'difference_from_fractional': {
                'final_nav_usd': float(nav.iloc[-1] - reference.curve.nav.iloc[-1]),
                'final_nav_relative': float(nav.iloc[-1] / reference.curve.nav.iloc[-1] - 1),
                'maximum_daily_nav_gap_usd': float(comparison.oms_minus_fractional_usd.abs().max()),
                'first_different_day': str(comparison.index[comparison.oms_minus_fractional_usd.abs() > 1e-8][0].date()),
                'total_fee_usd': oms_metrics['total_fees'] - fractional_metrics['total_fees']},
            'integer_decision_reasons': dict(Counter(decisions.reason)),
            'fractional_decision_reasons': dict(Counter(reference.decisions.reason)) if cost_aware else {'daily': len(actual) - 1},
            'idle_cash_mean_weight': float((actual.cash.iloc[1:] / nav.iloc[1:]).mean()),
            'idle_cash_max_weight': float((actual.cash.iloc[1:] / nav.iloc[1:]).max()),
            'database': str(db_path.resolve())}
        for name, frame, indexed in (
            ('fractional_curve', reference.curve, True), ('fractional_trades', reference.trades, False),
            ('fractional_weights', reference.holdings, True), ('integer_math_curve', expected, True),
            ('integer_order_plan', plan, False), ('integer_decisions', decisions, False),
            ('oms_curve', actual, True), ('oms_orders', fills, False), ('daily_comparison', comparison, True)):
            write_frame(directory / f'{name}.csv', frame, indexed)
        (directory / 'audit.json').write_text(json.dumps(audit, indent=2) + '\n')
        print(f'{strategy}: PASS, fractional ${reference.curve.nav.iloc[-1]:,.2f}; OMS ${nav.iloc[-1]:,.2f}', flush=True)
    root = Path(__file__).resolve().parents[1]
    provenance = {
        'created_at_utc': datetime.now(UTC).isoformat(), 'config': config.to_dict(),
        'oms_executed': True, 'transport': 'direct Store.create/accept/fill; same order methods used by the API',
        'quantity_model': 'floor target fractional adjusted-price units to integer units; sell before buy',
        'risk_free_asset_allocation': 0, 'idle_cash_interest': 'same daily RF as the existing mathematical ledger',
        'portfolio_accounting': 'oms.__main__ strategy loop; Store persists orders only',
        'price_model': 'frozen adjusted closes; research units, not raw tradable shares; no slippage',
        'timing': 'prior-session close signal; following-session close execution; no terminal liquidation',
        'historical_dates': 'CSV execution_date; OMS time is the actual UTC replay timestamp',
        'data_manifest_sha256': sha256(data_path / 'manifest.json'),
        'data_retrieved_at_utc': manifest['retrieved_at_utc'],
        'source_sha256': {str(p.relative_to(root)): sha256(p) for folder in ('oms', 'test') for p in sorted((root / folder).glob('*.py'))},
        'python': platform.python_version(),
        'libraries': {n: importlib.metadata.version(n) for n in ('numpy', 'pandas', 'scipy', 'fastapi', 'pydantic')},
        'elapsed_seconds': time.monotonic() - started}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False) + '\n')
    (output / 'run.json').write_text(json.dumps(provenance, indent=2, allow_nan=False) + '\n')
    hashes = {str(p.relative_to(output)): sha256(p) for p in sorted(output.rglob('*')) if p.is_file()}
    (output / 'checksums.json').write_text(json.dumps(hashes, indent=2) + '\n')
    print(f'Completed in {provenance["elapsed_seconds"]:.1f}s. Results: {output.resolve()}', flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('configs/daily-capm.json'))
    parser.add_argument('--data', type=Path, default=Path('datasets'))
    parser.add_argument('--output', type=Path, default=Path('results/oms-audit-lambda5'))
    parser.add_argument('--strategy', choices=['both', 'capm_cost_aware', 'capm_mean_variance'], default='both')
    parser.add_argument('--end', help='Optional shortened end date for a smoke run.')
    args = parser.parse_args()
    strategies = ['capm_cost_aware', 'capm_mean_variance'] if args.strategy == 'both' else [args.strategy]
    run(args.config, args.data, args.output, strategies, args.end)


if __name__ == '__main__':
    main()
