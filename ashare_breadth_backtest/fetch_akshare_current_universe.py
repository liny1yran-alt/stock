"""Optional public-data starter downloader using AkShare.

WARNING: stock_info_a_code_name() is a current universe. This download is for smoke tests
and prototyping only; it is not survivorship-bias-free historical A-share data.
Check provider terms, rate limits, corporate actions and missing/delisted coverage.
"""
import argparse, time
from pathlib import Path
import pandas as pd


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--start', default='20150101')
    p.add_argument('--end', default=pd.Timestamp.today().strftime('%Y%m%d'))
    p.add_argument('--out', default='data/current_universe_daily.csv')
    p.add_argument('--limit', type=int, default=0, help='仅下载前N只股票用于测试；0表示全部（可能耗时很长）')
    p.add_argument('--pause', type=float, default=0.25)
    args = p.parse_args()
    try:
        import akshare as ak
    except ImportError as e:
        raise SystemExit('缺少依赖，请先执行 pip install akshare') from e
    universe = ak.stock_info_a_code_name()
    if args.limit > 0:
        universe = universe.head(args.limit)
    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    first = True; failed = []
    for n, row in enumerate(universe.itertuples(index=False), 1):
        code = str(getattr(row, '代码', row[0])).zfill(6)
        try:
            hist = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=args.start, end_date=args.end, adjust='hfq')
            if hist is None or hist.empty:
                failed.append((code, 'empty')); continue
            # AkShare Chinese columns: 日期、开盘、收盘、最高、最低、成交量、成交额...
            colmap = {'日期':'date','开盘':'open','收盘':'close','最高':'high','最低':'low','成交量':'volume','成交额':'amount'}
            hist = hist.rename(columns=colmap)
            required = {'date','open','close'}
            if not required.issubset(hist.columns):
                failed.append((code, 'schema:' + ','.join(hist.columns))); continue
            hist['code'] = code
            hist.to_csv(out, mode='w' if first else 'a', header=first, index=False, encoding='utf-8-sig')
            first = False
        except Exception as e:
            failed.append((code, str(e)[:180]))
        if n % 100 == 0:
            print(f'{n}/{len(universe)} processed; failed={len(failed)}')
        time.sleep(args.pause)
    pd.DataFrame(failed, columns=['code','error']).to_csv(out.with_name('download_failures.csv'), index=False, encoding='utf-8-sig')
    print(f'完成。输出: {out}; 失败: {len(failed)}; 总股票: {len(universe)}')
    print('重要：这是当前股票池的复权行情，存在幸存者偏差；不可据此声称完成无偏全A股历史回测。')

if __name__ == '__main__': main()
