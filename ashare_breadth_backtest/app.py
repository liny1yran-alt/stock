import io, json
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
from engine import (BacktestConfig, validate_stock_bars, validate_target_bars, compute_breadth,
                    make_signals, run_backtest, data_quality_report, sha256_bytes)

st.set_page_config(page_title="A股市场宽度回测系统", page_icon="📈", layout="wide")
st.title("A股市场宽度 + BTI 回测系统")
st.caption("可复现研究工具｜信号在收盘后确认、下一交易日开盘执行｜不伪造数据或回测结果")

with st.expander("使用前必读：数据口径与限制", expanded=True):
    st.markdown("""
- **当前没有内置虚构行情。** 上传真实 CSV 后才会显示回测结果。
- 股票日线格式：`date, code, close`，建议同时提供 `open, high, low, volume, amount`；如有 `adj_close`，系统优先用它计算均线。
- 交易标的格式：`date, open, close`，建议提供 `high, low, volume, amount`。优先使用真实可交易宽基 ETF；若上传指数，只能视为理论代理，不代表可成交收益。
- 只含当前仍上市股票的数据会有**幸存者偏差**；全市场结论应纳入历史退市股票，并按历史日期重建股票池。
- 默认 BTI 是透明标注的标准 Zweig 近似实现。它不等于截图里的 BTI，除非原始公式得到确认。
- 默认交易费用与滑点是可编辑假设，不代表实际券商费率；请做成本敏感性测试。
""")

st.sidebar.header("回测参数")
ma_window = st.sidebar.number_input("均线周期（日）", min_value=5, max_value=250, value=50, step=1)
oversold = st.sidebar.number_input("极端低位阈值（%）", min_value=1.0, max_value=30.0, value=10.0, step=1.0)
lookback = st.sidebar.number_input("低位事件回看（日）", min_value=1, max_value=60, value=20, step=1)
reclaim = st.sidebar.number_input("收复阈值（%）", min_value=1.0, max_value=30.0, value=10.0, step=1.0)
holding = st.sidebar.selectbox("持有期限", [5, 20], index=0)
bti_mode_label = st.sidebar.selectbox("BTI定义", ["标准 Zweig 近似（40→61.5）", "自定义代理（上穿40）", "不使用BTI过滤"], index=0)
require_bti = bti_mode_label != "不使用BTI过滤"
bti_mode = "standard_zweig" if bti_mode_label.startswith("标准") else "proxy_40"
fee = st.sidebar.number_input("单边手续费（基点）", min_value=0.0, max_value=100.0, value=3.0, step=0.5)
slip = st.sidebar.number_input("单边滑点（基点）", min_value=0.0, max_value=200.0, value=5.0, step=1.0)
position = st.sidebar.slider("每次最大仓位比例", min_value=0.1, max_value=1.0, value=1.0, step=0.1)
initial_cash = st.sidebar.number_input("初始资金（元）", min_value=1000.0, value=1_000_000.0, step=100_000.0)

st.subheader("1. 导入真实数据")
input_mode = st.radio("宽度数据来源", ["由个股日线计算全市场宽度（推荐）", "上传已计算的宽度序列"], horizontal=True)
stock_file = breadth_file = None
if input_mode.startswith("由个股"):
    stock_file = st.file_uploader("上传A股股票级日线 CSV", type=["csv"], key="stocks", help="必需字段 date, code, close；可选 open/high/low/volume/amount/adj_close")
else:
    breadth_file = st.file_uploader("上传每日宽度 CSV", type=["csv"], key="breadth", help="必需 date,breadth_pct；若要BTI确认还需要 advancers,decliners 或 ad_ratio_pct")
target_file = st.file_uploader("上传执行标的日线 CSV（ETF优先）", type=["csv"], key="target", help="必需 date,open,close；可选 high/low/volume/amount")
target_name = st.text_input("执行标的名称/代码", value="请填写ETF代码或指数代理")

if st.button("运行回测", type="primary", use_container_width=True):
    if target_file is None or (stock_file is None and breadth_file is None):
        st.error("请上传宽度数据（股票级日线或已计算宽度序列）和执行标的日线后再运行。")
        st.stop()
    try:
        target_raw = pd.read_csv(target_file)
        target, target_q = validate_target_bars(target_raw)
        stock_q = {}
        if stock_file is not None:
            stock_raw = pd.read_csv(stock_file)
            stock_valid, stock_q = validate_stock_bars(stock_raw)
            breadth = compute_breadth(stock_valid, int(ma_window))
        else:
            breadth = pd.read_csv(breadth_file)
            if not {"date", "breadth_pct"}.issubset(breadth.columns):
                raise ValueError("宽度 CSV 必须包含 date,breadth_pct。")
            breadth["date"] = pd.to_datetime(breadth["date"], errors="coerce").dt.normalize()
            if breadth.date.isna().any(): raise ValueError("宽度 CSV 的 date 有无法解析的值。")
            breadth["breadth_pct"] = pd.to_numeric(breadth["breadth_pct"], errors="coerce")
            if "ad_ratio_pct" not in breadth:
                if {"advancers", "decliners"}.issubset(breadth.columns):
                    denom = pd.to_numeric(breadth.advancers, errors="coerce") + pd.to_numeric(breadth.decliners, errors="coerce")
                    breadth["ad_ratio_pct"] = pd.to_numeric(breadth.advancers, errors="coerce") / denom * 100
                else:
                    breadth["ad_ratio_pct"] = float("nan")
            breadth = breadth.dropna(subset=["date", "breadth_pct"]).sort_values("date")
            stock_q = {"source": "用户上传的预计算宽度序列", "rows": len(breadth), "codes": None,
                       "warning": "无法从汇总序列独立复核宽度公式；请保留源文件和公式说明。"}
        cfg = BacktestConfig(ma_window=int(ma_window), breadth_oversold=float(oversold),
             oversold_lookback=int(lookback), breadth_reclaim=float(reclaim), bti_mode=bti_mode,
             holding_days=int(holding), fee_bps_each_side=float(fee), slippage_bps_each_side=float(slip),
             initial_cash=float(initial_cash), max_position_fraction=float(position), require_bti=require_bti)
        signals = make_signals(breadth, cfg)
        trades, curve, metrics = run_backtest(signals, target, cfg)
        quality = data_quality_report(stock_q, breadth, signals, target_q)
        st.session_state["results"] = {"breadth": breadth, "signals": signals, "trades": trades,
                                        "curve": curve, "metrics": metrics, "quality": quality,
                                        "target_name": target_name, "cfg": cfg}
        st.success("回测完成。请先查看数据质量与样本数量，再解释收益指标。")
    except Exception as e:
        st.error(f"回测未完成：{e}")

if "results" in st.session_state:
    r = st.session_state["results"]
    m = r["metrics"]
    st.subheader("2. 回测摘要")
    st.caption(f"执行标的：{r['target_name']}｜持有期限：{m['config']['holding_days']}个交易日｜净收益已按输入的单边手续费与滑点假设扣减")
    if m["trades"] == 0:
        st.warning("本次规则没有产生可执行交易。请检查 BTI 数据、宽度阈值、日期重叠与输入范围；不要为了产生交易而事后随意调参。")
    cols = st.columns(4)
    vals = [("独立交易数", f"{m['trades']}"), ("胜率", f"{m['win_rate_pct']:.2f}%" if pd.notna(m['win_rate_pct']) else "N/A"),
            ("单笔平均净收益", f"{m['mean_trade_net_pct']:.2f}%" if pd.notna(m['mean_trade_net_pct']) else "N/A"),
            ("最大回撤", f"{m['max_drawdown_pct']:.2f}%")]
    for c,(label,value) in zip(cols, vals): c.metric(label, value)
    cols = st.columns(4)
    vals = [("累计净值收益", f"{m['total_return_pct']:.2f}%"), ("年化收益", f"{m['annualized_return_pct']:.2f}%" if pd.notna(m['annualized_return_pct']) else "N/A"),
            ("年化波动", f"{m['annualized_volatility_pct']:.2f}%" if pd.notna(m['annualized_volatility_pct']) else "N/A"),
            ("夏普（无风险利率0）", f"{m['sharpe_rf0']:.2f}" if pd.notna(m['sharpe_rf0']) else "N/A")]
    for c,(label,value) in zip(cols, vals): c.metric(label, value)
    st.subheader("3. 净值与市场宽度")
    fig, ax = plt.subplots(figsize=(12,4)); ax.plot(r["curve"].date, r["curve"].nav); ax.set_title("策略净值（现金空仓期收益为0）"); ax.set_ylabel("净值（元）"); ax.grid(True, alpha=.25); st.pyplot(fig); plt.close(fig)
    fig, ax = plt.subplots(figsize=(12,3)); ax.plot(r["signals"].date, r["signals"].breadth_pct, label="50日均线占比"); ax.axhline(oversold, linestyle="--", label="极端低位阈值"); ax.axhline(reclaim, linestyle=":", label="收复阈值"); ax.set_ylim(0,100); ax.legend(); ax.grid(True, alpha=.25); st.pyplot(fig); plt.close(fig)
    st.subheader("4. 逐笔交易记录")
    st.dataframe(r["trades"], use_container_width=True, hide_index=True)
    st.subheader("5. 数据质量与复现信息")
    st.json(r["quality"])
    st.markdown("**解释限制**：当前回测仅对上传的数据有效。若股票池是当前存续股票、行情未经正确复权、执行标的是指数而非ETF，或BTI公式与原图不同，结果不能被解释为无偏的全A股可交易策略绩效。")
    def csv_bytes(df): return df.to_csv(index=False).encode("utf-8-sig")
    a,b,c,d = st.columns(4)
    a.download_button("下载宽度与BTI CSV", csv_bytes(r["signals"]), "breadth_signals.csv", "text/csv", use_container_width=True)
    b.download_button("下载交易记录 CSV", csv_bytes(r["trades"]), "trades.csv", "text/csv", use_container_width=True)
    c.download_button("下载净值曲线 CSV", csv_bytes(r["curve"]), "equity_curve.csv", "text/csv", use_container_width=True)
    d.download_button("下载质量报告 JSON", json.dumps(r["quality"], ensure_ascii=False, indent=2, default=str).encode("utf-8"), "data_quality_report.json", "application/json", use_container_width=True)

st.divider()
st.subheader("数据获取建议")
st.markdown("""
- [F10历史市场宽度](https://f10.org/index/sh/)：可做交叉核验，但其WMA/股票池口径与本系统的SMA全市场定义不保证一致。
- [A股全市场日线数据入口](https://money-tree.eyunzhu.com/)：获取历史日线/股票列表后，整理成系统要求的CSV格式。
- [Tushare](https://tushare.pro/)：适合通过授权接口获取股票基础信息、行情和交易日历；请确认历史退市覆盖及权限。

**最低数据质量门槛**：没有股票级历史数据时，不要声称已复算全A股宽度；没有原图BTI公式时，不要声称已复现原图BTI；没有可交易ETF价格时，不要把指数模拟收益写成真实可交易收益。
""")
