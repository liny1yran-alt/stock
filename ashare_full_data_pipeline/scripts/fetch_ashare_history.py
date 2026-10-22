#!/usr/bin/env python3
"""Download A-share daily bars using Baostock and calculate daily SMA50 breadth.

Important: this is a public-data research pipeline, not a guarantee of complete
point-in-time universe or corporate-action-correct executable returns.
"""
from __future__ import annotations
import argparse, json, time, sys
from pathlib import Path
from datetime import datetime
import pandas as pd

def parse_args():
    p=argparse.ArgumentParser()
    p.add_argument("--start", default="2016-08-08")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--out", default="data")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--compute-breadth", action="store_true")
    p.add_argument("--sleep", type=float, default=0.15, help="pause between requests")
    p.add_argument("--limit", type=int, default=0, help="debug: only process first N symbols")
    return p.parse_args()

def main():
    args=parse_args()
    start=pd.Timestamp(args.start); end=pd.Timestamp(args.end)
    if start>end: raise SystemExit("--start must be <= --end")
    out=Path(args.out); bars_dir=out/"stock_bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    import baostock as bs
    login=bs.login()
    if login.error_code != "0":
        raise SystemExit(f"Baostock login failed: {login.error_code} {login.error_msg}")
    errors=[]
    universe=[]
    try:
        # Current universe. Historical/delisted coverage must be separately verified.
        rs=bs.query_all_stock(end.strftime("%Y-%m-%d"))
        while rs.error_code=="0" and rs.next():
            row=rs.get_row_data()
            if len(row)>=2:
                code, trade_status = row[0], row[1]
                if code.startswith(("sh.","sz.")):
                    universe.append({"code":code,"trade_status_on_end_date":trade_status})
        if not universe:
            raise RuntimeError("No symbols returned by query_all_stock; verify provider status/API.")
        if args.limit: universe=universe[:args.limit]
        print(f"Universe rows from provider: {len(universe)}; date {end.date()}")
        done=0
        for j,item in enumerate(universe,1):
            code=item["code"]
            path=bars_dir/(code.replace(".","_")+".csv")
            if args.resume and path.exists() and path.stat().st_size>50:
                done+=1; continue
            try:
                rs=bs.query_history_k_data_plus(
                    code,
                    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST",
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end.strftime("%Y-%m-%d"),
                    frequency="d", adjustflag="2")
                data=[]
                while rs.error_code=="0" and rs.next():
                    data.append(rs.get_row_data())
                if rs.error_code!="0":
                    raise RuntimeError(f"{rs.error_code}: {rs.error_msg}")
                if data:
                    df=pd.DataFrame(data,columns=rs.fields)
                    for c in ["open","high","low","close","preclose","volume","amount"]:
                        df[c]=pd.to_numeric(df[c],errors="coerce")
                    df["date"]=pd.to_datetime(df["date"],errors="coerce")
                    df["provider_trade_status_on_end_date"]=item.get("trade_status_on_end_date","")
                    df.to_csv(path,index=False)
                    done+=1
                else:
                    errors.append({"code":code,"name":item.get("name",""),"error":"no rows"})
            except Exception as e:
                errors.append({"code":code,"name":item.get("name",""),"error":str(e)[:500]})
            if j%100==0:
                print(f"{j}/{len(universe)} processed; saved={done}; errors={len(errors)}")
            time.sleep(max(0,args.sleep))
    finally:
        bs.logout()
    err_df=pd.DataFrame(errors,columns=["code","name","error"])
    err_df.to_csv(out/"fetch_errors.csv",index=False,encoding="utf-8-sig")
    manifest={
      "provider":"Baostock",
      "requested_start":args.start,"requested_end":args.end,
      "universe_date":args.end,"symbols_returned":len(universe),
      "symbols_saved_or_skipped":done,"errors":len(errors),
      "price_basis":"adjustflag=2 (provider-defined forward-adjusted; verify current Baostock docs)",
      "data_status":"PARTIAL_UNTIL_VALIDATED",
      "limitations":[
        "Universe is retrieved as of end date, not point-in-time per historical date; survivor bias may remain.",
        "Provider may not return all delisted securities through query_all_stock; historical delisted coverage is not guaranteed.",
        "Adjusted prices are for breadth research; they are not executable historical fill prices.",
        "Suspension, limit-up/down and corporate-action handling require additional validation.",
        "Do not interpret this as the original screenshot BTI; only daily SMA50 breadth is computed here."
      ]
    }
    (out/"data_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(manifest,ensure_ascii=False,indent=2))
    if args.compute_breadth:
        compute_breadth(bars_dir,out)

def compute_breadth(bars_dir:Path,out:Path):
    frames=[]
    files=list(bars_dir.glob("*.csv"))
    print(f"Calculating SMA50 breadth from {len(files)} symbol files...")
    for path in files:
        try:
            df=pd.read_csv(path,parse_dates=["date"])
            if not {"date","close"}.issubset(df.columns): continue
            df=df.dropna(subset=["date","close"]).sort_values("date").drop_duplicates("date")
            df["sma50"]=df["close"].rolling(50,min_periods=50).mean()
            df["eligible"]=df["sma50"].notna()
            df["above50"]=(df["close"]>df["sma50"]) & df["eligible"]
            frames.append(df[["date","eligible","above50"]])
        except Exception as e:
            print(f"Skipping {path.name}: {e}",file=sys.stderr)
    if not frames:
        raise RuntimeError("No usable bar files to compute breadth.")
    all_df=pd.concat(frames,ignore_index=True)
    daily=all_df.groupby("date").agg(eligible_count=("eligible","sum"),above50_count=("above50","sum"))
    daily["breadth50"]=100*daily["above50_count"]/daily["eligible_count"].replace(0,pd.NA)
    daily=daily.reset_index().sort_values("date")
    daily.to_csv(out/"breadth50_daily.csv",index=False,encoding="utf-8-sig")
    print(f"Wrote {out/'breadth50_daily.csv'} ({len(daily)} dates)")
if __name__=="__main__": main()
