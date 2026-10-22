# A股全市场历史行情获取与宽度计算管道

本目录提供一个可复现的数据获取脚本。它**不会把当前股票名单冒充历史股票池**，并在输出中记录数据覆盖、缺失和口径限制。

## 数据源与限制
- 默认使用 Baostock 免费接口获取股票列表和日线数据；具体接口的可用性、限流和历史覆盖必须在运行时验证。
- 由于免费数据源可能无法稳定覆盖全部退市股票、历史交易状态、复权因子与涨跌停可成交性，脚本会在 manifest 中标记局限。
- 需要严格点时历史（PIT）股票池、退市股、可靠复权和商用许可时，建议切换 Tushare Pro / Wind / Choice / RiceQuant 等有权限的数据源。
- 本脚本不会在当前会话中自动访问或写入你的 GitHub；你需要把这些文件上传到 `liny1yran-alt/stock` 仓库，之后可在云端执行。

## 本地运行
```bash
pip install -r requirements.txt
python scripts/fetch_ashare_history.py --start 2016-08-08 --end 2026-10-21 --out data
```
首次全量下载可能耗时数小时甚至更久。脚本会按股票保存 CSV，失败会记录在 `data/fetch_errors.csv`，重复运行时可使用 `--resume` 跳过已完成股票。

## 计算每日50日均线占比
```bash
python scripts/fetch_ashare_history.py --start 2016-08-08 --end 2026-10-21 --out data --compute-breadth
```
输出：
- `data/stock_bars/*.csv`：个股日线（按证券分文件）
- `data/breadth50_daily.csv`：每日站上50日简单均线的股票占比
- `data/data_manifest.json`：数据源、区间、行数、股票数、失败数和重要限制
- `data/fetch_errors.csv`：失败清单

## 策略数据口径
50日占比使用每只股票自己的有效收盘价序列，收盘价严格大于50日简单移动平均时计入分子；当日可用且满足50日历史数据的股票计入分母。若用未复权价格，除权除息可能扭曲均线；若用回溯调整价格，交易收益必须另外使用可交易原始价格和公司行为数据。请不要把本输出直接当作可成交策略收益。

## GitHub Actions
workflow 手动运行需要 GitHub Actions 有效额度；全量下载可能超时，且 Actions artifact 有保留期限，不建议作为长期数据仓库。先运行短区间验证，确认接口可用后再延长日期。
