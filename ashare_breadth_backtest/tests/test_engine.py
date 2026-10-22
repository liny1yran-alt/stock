import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import numpy as np
from engine import BacktestConfig, compute_breadth, make_signals, run_backtest, validate_stock_bars


def test_breadth_uses_rolling_sma_and_valid_denominator():
    dates = pd.bdate_range('2024-01-01', periods=60)
    rows = []
    for code, mult in [('A', 1.0), ('B', 2.0)]:
        for i, d in enumerate(dates):
            price = (100 + i) * mult
            rows.append({'date': d, 'code': code, 'close': price})
    breadth = compute_breadth(pd.DataFrame(rows), ma_window=5)
    assert breadth.iloc[0].eligible_count == 2
    assert breadth.iloc[0].breadth_pct == 100.0


def test_target_validation_drops_duplicate_dates():
    from engine import validate_target_bars
    d = pd.DataFrame({'date':['2024-01-01','2024-01-01'], 'open':[10,11], 'close':[10,11]})
    clean, q = validate_target_bars(d)
    assert len(clean) == 0
    assert q['duplicate_dates'] == 1


def test_backtest_no_signals_does_not_crash():
    dates = pd.bdate_range('2024-01-01', periods=30)
    breadth = pd.DataFrame({'date':dates, 'breadth_pct':[50.0]*30, 'eligible_count':[100]*30,
                            'above_count':[50]*30, 'advancers':[60]*30, 'decliners':[40]*30,
                            'unchanged':[0]*30, 'ad_ratio_pct':[60.0]*30})
    cfg = BacktestConfig(holding_days=5)
    sig = make_signals(breadth, cfg)
    target = pd.DataFrame({'date':dates, 'open':np.arange(30)+100., 'close':np.arange(30)+101., 'low':np.arange(30)+99.})
    trades, curve, metrics = run_backtest(sig, target, cfg)
    assert len(trades) == 0
    assert metrics['trades'] == 0
    assert len(curve) == 30
