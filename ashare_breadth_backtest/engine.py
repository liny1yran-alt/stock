"""Reproducible A-share breadth backtest engine; no hidden network calls."""
from dataclasses import dataclass, asdict
import hashlib, json
import numpy as np
import pandas as pd

@dataclass
class BacktestConfig:
    ma_window: int = 50
    breadth_oversold: float = 10.0
    oversold_lookback: int = 20
    breadth_reclaim: float = 10.0
    bti_mode: str = "standard_zweig"  # standard_zweig or proxy_40
    bti_ema_span: int = 10
    holding_days: int = 5
    fee_bps_each_side: float = 3.0
    slippage_bps_each_side: float = 5.0
    initial_cash: float = 1_000_000.0
    max_position_fraction: float = 1.0
    require_bti: bool = True


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _dates(df, col="date"):
    x = df.copy()
    x[col] = pd.to_datetime(x[col], errors="coerce").dt.normalize()
    if x[col].isna().any():
        raise ValueError(f"{col} 存在无法解析的日期")
    return x


def validate_stock_bars(df):
    required = {"date", "code", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"股票行情必须包含字段：{sorted(required)}")
    x = _dates(df)
    x["code"] = x["code"].astype(str).str.strip()
    for c in ["close", "open", "high", "low", "volume", "amount", "adj_close"]:
        if c in x: x[c] = pd.to_numeric(x[c], errors="coerce")
    dupes = int(x.duplicated(["date", "code"]).sum())
    invalid = int((x.close.isna() | (x.close <= 0)).sum())
    x = x.drop_duplicates(["date", "code"], keep=False)
    x = x[x.close.notna() & (x.close > 0)].copy()
    pcol = "adj_close" if "adj_close" in x and x.adj_close.notna().any() else "close"
    x["price_for_ma"] = x[pcol]
    x = x.sort_values(["code", "date"])
    q = {"rows_input": len(df), "rows_valid": len(x), "duplicate_date_code_rows": dupes,
         "invalid_close_rows": invalid, "codes": int(x.code.nunique()),
         "date_min": str(x.date.min().date()) if len(x) else None,
         "date_max": str(x.date.max().date()) if len(x) else None,
         "price_field_for_MA": pcol,
         "adjustment_warning": "使用adj_close" if pcol == "adj_close" else "使用close原始价格；公司行为可能影响均线准确性"}
    return x, q


def validate_target_bars(df):
    required = {"date", "open", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"交易标的行情必须包含字段：{sorted(required)}")
    x = _dates(df)
    for c in ["open", "close", "high", "low", "volume", "amount"]:
        if c in x: x[c] = pd.to_numeric(x[c], errors="coerce")
    dupes = int(x.duplicated(["date"]).sum())
    x = x.sort_values("date").drop_duplicates("date", keep=False)
    invalid = int((x.open.isna() | x.close.isna() | (x.open <= 0) | (x.close <= 0)).sum())
    x = x[x.open.notna() & x.close.notna() & (x.open > 0) & (x.close > 0)].copy().reset_index(drop=True)
    q = {"rows_input": len(df), "rows_valid": len(x), "duplicate_dates": dupes,
         "invalid_open_close_rows": invalid, "date_min": str(x.date.min().date()) if len(x) else None,
         "date_max": str(x.date.max().date()) if len(x) else None}
    return x, q


def compute_breadth(stock_bars, ma_window=50):
    x, _ = validate_stock_bars(stock_bars)
    x["sma"] = x.groupby("code", sort=False).price_for_ma.transform(lambda s: s.rolling(ma_window, min_periods=ma_window).mean())
    x["eligible"] = x.sma.notna() & x.price_for_ma.notna()
    x["above"] = (x.price_for_ma > x.sma) & x.eligible
    x["prev_price"] = x.groupby("code", sort=False).price_for_ma.shift(1)
    x["chg"] = x.price_for_ma / x.prev_price - 1
    e = x[x.eligible].groupby("date").agg(eligible_count=("code", "nunique"), above_count=("above", "sum"))
    e["breadth_pct"] = e.above_count / e.eligible_count * 100
    for mask, name in [(x.chg > 0, "advancers"), (x.chg < 0, "decliners"), (x.chg == 0, "unchanged")]:
        e = e.join(x[mask].groupby("date").code.nunique().rename(name), how="left")
    e[["advancers", "decliners", "unchanged"]] = e[["advancers", "decliners", "unchanged"]].fillna(0)
    denom = e.advancers + e.decliners
    e["ad_ratio_pct"] = np.where(denom > 0, e.advancers / denom * 100, np.nan)
    return e.reset_index().sort_values("date").reset_index(drop=True)


def make_signals(breadth, cfg):
    x = breadth.copy().sort_values("date").reset_index(drop=True)
    # Wilder/Zweig-style EMA of advancing issues / (advancing + declining issues), in percentage points.
    x["bti"] = x.ad_ratio_pct.ewm(span=cfg.bti_ema_span, adjust=False, min_periods=cfg.bti_ema_span).mean()
    if cfg.bti_mode == "standard_zweig":
        low_recent = x.bti.shift(1).rolling(10, min_periods=1).min() <= 40
        x["bti_confirm"] = low_recent & (x.bti.shift(1) <= 61.5) & (x.bti > 61.5)
        x["bti_definition"] = "近似Zweig：EMA10上涨家数/(上涨+下跌)，10日内低于/等于40后上穿61.5"
    else:
        x["bti_confirm"] = (x.bti.shift(1) <= 40) & (x.bti > 40)
        x["bti_definition"] = "自定义代理：EMA10上涨广度上穿40；不等于标准Zweig或原图公式"
    x["recent_oversold"] = x.breadth_pct.shift(1).rolling(cfg.oversold_lookback, min_periods=1).min() < cfg.breadth_oversold
    x["breadth_reclaim"] = (x.breadth_pct.shift(1) <= cfg.breadth_reclaim) & (x.breadth_pct > cfg.breadth_reclaim)
    x["signal"] = x.recent_oversold & x.breadth_reclaim
    if cfg.require_bti: x["signal"] &= x.bti_confirm.fillna(False)
    x["signal_reason"] = np.where(x.signal, "低位事件+占比收复阈值" + ("+BTI确认" if cfg.require_bti else ""), "")
    return x


def _one_trade(target, entry_idx, exit_idx, cfg):
    entry = float(target.iloc[entry_idx].open); exitp = float(target.iloc[exit_idx].close)
    cost = (cfg.fee_bps_each_side + cfg.slippage_bps_each_side) / 10000
    gross = exitp / entry - 1
    net = exitp * (1-cost) / (entry * (1+cost)) - 1
    lowseries = target.iloc[entry_idx:exit_idx+1].low if "low" in target else target.iloc[entry_idx:exit_idx+1].close
    mae = pd.to_numeric(lowseries, errors="coerce").min() / entry - 1
    return gross, net, mae


def summarize(trades, curve, cfg, target_quality):
    r = trades.net_return_pct / 100 if len(trades) else pd.Series(dtype=float)
    wins = r[r > 0]; losses = r[r < 0]
    net_total = float(curve.nav.iloc[-1] / cfg.initial_cash - 1) if len(curve) else 0.0
    years = max((curve.date.max() - curve.date.min()).days / 365.25, 1/365.25) if len(curve) else 0
    ann = (curve.nav.iloc[-1] / cfg.initial_cash) ** (1/years) - 1 if len(curve) and years > 0 else np.nan
    daily = curve.strategy_return if len(curve) else pd.Series(dtype=float)
    vol = float(daily.std(ddof=1) * np.sqrt(252)) if len(daily) > 1 else np.nan
    sharpe = float(daily.mean() / daily.std(ddof=1) * np.sqrt(252)) if len(daily) > 1 and daily.std(ddof=1) > 0 else np.nan
    return {"trades": int(len(trades)), "win_rate_pct": float((r > 0).mean()*100) if len(r) else np.nan,
            "mean_trade_net_pct": float(r.mean()*100) if len(r) else np.nan,
            "median_trade_net_pct": float(r.median()*100) if len(r) else np.nan,
            "avg_win_pct": float(wins.mean()*100) if len(wins) else np.nan,
            "avg_loss_pct": float(losses.mean()*100) if len(losses) else np.nan,
            "profit_factor": float(wins.sum()/abs(losses.sum())) if len(losses) and abs(losses.sum()) > 0 else np.nan,
            "total_return_pct": net_total*100, "annualized_return_pct": ann*100 if pd.notna(ann) else np.nan,
            "max_drawdown_pct": float(curve.drawdown_pct.min()) if len(curve) else 0,
            "annualized_volatility_pct": vol*100 if pd.notna(vol) else np.nan,
            "sharpe_rf0": sharpe, "initial_cash": cfg.initial_cash,
            "final_nav": float(curve.nav.iloc[-1]) if len(curve) else cfg.initial_cash,
            "target_quality": target_quality, "config": asdict(cfg)}


def run_backtest(signals, target_bars, cfg):
    target, tq = validate_target_bars(target_bars)
    s = signals.sort_values("date").reset_index(drop=True)
    trades = []; busy_until = -1
    tdates = target.date.to_numpy(dtype="datetime64[ns]")
    for _, row in s[s.signal.fillna(False)].iterrows():
        sig = pd.Timestamp(row.date).to_datetime64()
        entries = np.flatnonzero(tdates > sig)
        if not len(entries): continue
        e = int(entries[0]); z = e + cfg.holding_days - 1
        if z >= len(target) or e <= busy_until: continue
        gross, net, mae = _one_trade(target, e, z, cfg)
        trades.append({"signal_date": row.date, "entry_date": target.iloc[e].date, "entry_open": target.iloc[e].open,
                       "exit_date": target.iloc[z].date, "exit_close": target.iloc[z].close,
                       "holding_days": cfg.holding_days, "gross_return_pct": gross*100, "net_return_pct": net*100,
                       "mae_pct": mae*100, "breadth_pct": row.get("breadth_pct", np.nan), "bti": row.get("bti", np.nan),
                       "signal_reason": row.get("signal_reason", "")})
        busy_until = z
    trades = pd.DataFrame(trades)
    if trades.empty:
        trades = pd.DataFrame(columns=["signal_date", "entry_date", "entry_open", "exit_date", "exit_close", "holding_days", "gross_return_pct", "net_return_pct", "mae_pct", "breadth_pct", "bti", "signal_reason"])
    target["strategy_return"] = 0.0; target["position"] = 0.0
    cost = (cfg.fee_bps_each_side + cfg.slippage_bps_each_side)/10000
    for _, tr in trades.iterrows():
        e = int(target.index[target.date == tr.entry_date][0]); z = int(target.index[target.date == tr.exit_date][0])
        for j in range(e, z+1):
            r = target.iloc[j].close/target.iloc[j].open-1 if j == e else target.iloc[j].close/target.iloc[j-1].close-1
            target.loc[j, "strategy_return"] = r * cfg.max_position_fraction
            target.loc[j, "position"] = cfg.max_position_fraction
        target.loc[e, "strategy_return"] -= cost * cfg.max_position_fraction
        target.loc[z, "strategy_return"] -= cost * cfg.max_position_fraction
    target["nav"] = cfg.initial_cash * (1 + target.strategy_return).cumprod()
    target["running_peak"] = target.nav.cummax()
    target["drawdown_pct"] = (target.nav/target.running_peak - 1)*100
    metrics = summarize(trades, target, cfg, tq)
    return trades, target, metrics


def data_quality_report(stock_quality, breadth, signals, target_quality=None):
    sig = int(signals.signal.fillna(False).sum()) if len(signals) else 0
    return {"stock_data": stock_quality, "target_data": target_quality or {},
            "breadth_rows": int(len(breadth)), "breadth_dates": [str(breadth.date.min().date()), str(breadth.date.max().date())] if len(breadth) else [],
            "breadth_min_eligible": int(breadth.eligible_count.min()) if len(breadth) else 0,
            "breadth_max_eligible": int(breadth.eligible_count.max()) if len(breadth) else 0,
            "signals_generated": sig,
            "survivorship_bias_warning": "若输入仅包含当前上市股票，则历史结果有幸存者偏差；需纳入历史退市股和点时股票池。",
            "bti_warning": "BTI基于每日涨跌家数构造的代理指标；未拿到原图公式时，不能声称复现原图BTI。"}
