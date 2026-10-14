# -*- coding: utf-8 -*-
"""2026 HK143 price-only bottom pattern comparison; no lookahead or automatic trades."""
from collections import defaultdict, Counter
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import csv, json, math
import pandas as pd
import exchange_calendars as xc

ROOT=Path(__file__).resolve().parent
CAL=xc.get_calendar("XHKG")
PATTERNS={
 "BASE_LOW":"低位+120日下跌≥20%（基线）",
 "NEW_LOW":"创20日新低且收盘未收复",
 "FAKE_2B":"2B跌破前低后收回",
 "HIGHER_RETEST":"二次回踩不破前低",
 "REVERSAL":"新低后强势阳线",
 "LOCAL_BREAK":"收盘突破前三日高点",
 "CONFIRMED_2B":"2B后次日再突破确认",
}
def load_prices(code):
    pieces=[]
    for year in [2025,2026]:
        f=ROOT/"data"/str(year)/f"{code}_{year}.csv"
        if not f.exists(): return None
        t=pd.read_csv(f,dtype={"date":str},encoding="utf-8-sig")
        if not {"date","open","high","low","close"}.issubset(t.columns):return None
        pieces.append(t[["date","open","high","low","close"]])
    t=pd.concat(pieces).drop_duplicates("date").sort_values("date")
    result={}
    for x in t.itertuples(index=False):
        vals=(float(x.open),float(x.high),float(x.low),float(x.close))
        if all(math.isfinite(v) and v>0 for v in vals):
            result[x.date]=vals
    return result
def zone(price,lo,hi):
    return hi>lo>0 and (price-lo)/(hi-lo)<=.20 and price/lo-1<=.15
def future_result(dates,lookup,i,entry):
    for n in range(1,21):
        bar=lookup.get(dates[i+n])
        if bar is None:return None
        if bar[2]<=entry*.92:
            return ("SAME_BAR_AMBIGUOUS_LOSS" if bar[1]>=entry*1.05 else "STOP_FIRST",n)
        if bar[1]>=entry*1.05:return ("TARGET_FIRST",n)
    return ("NO_TARGET",20)
def ci_wilson(w,n):
    if not n:return [None,None]
    z=1.96;p=w/n;den=1+z*z/n
    mid=(p+z*z/(2*n))/den
    diff=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [round(100*max(0,mid-diff),1),round(100*min(1,mid+diff),1)]
def main():
    with (ROOT/"config"/"stocks.csv").open(encoding="utf-8-sig",newline="") as f:
        stocks=[(r["code"].zfill(5),r["name"]) for r in csv.DictReader(f)]
    assert len(stocks)==143 and len({code for code,name in stocks})==143
    with (ROOT/"data"/"latest.json").open(encoding="utf-8") as f:
        cutoff=json.load(f)["meta"]["target_session_hkt"]
    dates=[x.date().isoformat() for x in CAL.sessions_in_range(pd.Timestamp("2025-01-01"),pd.Timestamp(cutoff))]
    year25={d for d in dates if d.startswith("2025")}
    events=defaultdict(list); skipped=Counter(); bottom_days=0
    for code,name in stocks:
        data=load_prices(code)
        if data is None:skipped["missing_history_files"]+=1;continue
        previous=[data[d] for d in dates if d.startswith("2025") and d in data]
        if not previous:skipped["missing_previous_year"]+=1;continue
        prev_full=year25.issubset(data.keys())
        low25=min(x[2] for x in previous)
        high25=max(x[1] for x in previous)
        low26=math.inf;high26=-math.inf;cur_full=True
        previous_2b=False
        last_signal={s:-999 for s in PATTERNS}
        for i,today in enumerate(dates):
            if not today.startswith("2026"):continue
            bar=data.get(today)
            if bar is None:
                cur_full=False;previous_2b=False;continue
            op,hi,lo,cl=bar
            low26=min(low26,lo);high26=max(high26,hi)
            if i<120 or i+20>=len(dates):
                previous_2b=False;continue
            prev20=[data.get(d) for d in dates[i-20:i]]
            if None in prev20 or dates[i-120] not in data or dates[i-1] not in data:
                previous_2b=False;skipped["missing_lookback"]+=1;continue
            old20=min(x[2] for x in prev20)
            ago=20-next(j for j,x in enumerate(prev20) if x[2]==old20)
            yest=data[dates[i-1]]
            is2b=(5<=ago<=20 and old20*.97<=lo<old20 and cl>old20)
            yesterday_2b=previous_2b
            previous_2b=is2b
            if cl/data[dates[i-120]][3]-1>-.20:continue
            low_previous=prev_full and zone(cl,low25,high25)
            low_this=cur_full and zone(cl,low26,high26)
            if not (low_previous or low_this):continue
            # Exclude stock windows with potentially noncomparable unadjusted prices.
            gap=any(abs(data[dates[j]][0]/data[dates[j-1]][3]-1)>.45 for j in range(i-119,i+1)
                    if dates[j] in data and dates[j-1] in data)
            if gap:skipped["potential_corporate_action_gap"]+=1;continue
            bottom_days+=1
            reverse=(lo<old20 and cl>op and cl>yest[3] and hi>lo and (cl-lo)/(hi-lo)>=.70)
            retest=(5<=ago<=20 and old20<=lo<=old20*1.02 and cl>op and cl>yest[3])
            recent_near=min(data[d][2] for d in dates[i-5:i+1])<=old20*1.03
            strength=(cl>op and cl>max(data[d][1] for d in dates[i-3:i]) and recent_near)
            flags={
                "BASE_LOW":True,
                "NEW_LOW":lo<old20 and cl<=old20,
                "FAKE_2B":is2b,
                "HIGHER_RETEST":retest,
                "REVERSAL":reverse,
                "LOCAL_BREAK":strength,
                "CONFIRMED_2B":yesterday_2b and cl>yest[1] and cl>op,
            }
            next_bar=data.get(dates[i+1])
            if next_bar is None:skipped["missing_next_day_open"]+=1;continue
            for kind,flag in flags.items():
                if not flag or i-last_signal[kind]<=20:continue
                out=future_result(dates,data,i,next_bar[0])
                if out is None:skipped["missing_future_bars"]+=1;continue
                last_signal[kind]=i
                result,horizon=out
                events[kind].append({"code":code,"name":name,"signal_date":today,
                    "entry_date":dates[i+1],"entry_open":round(next_bar[0],5),
                    "group":"2025&2026" if low_previous and low_this else "2025" if low_previous else "2026",
                    "sample":"development" if today<"2026-07-01" else "holdout",
                    "result":result,"days":horizon})
    summary={}
    for kind,label in PATTERNS.items():
        subsets={}
        for tag in ["development","holdout","all"]:
            group=[v for v in events[kind] if tag=="all" or v["sample"]==tag]
            n=len(group);wins=sum(x["result"]=="TARGET_FIRST" for x in group)
            subsets[tag]={"n":n,"wins":wins,"rate_pct":round(100*wins/n,1) if n else None,
                "wilson95_pct":ci_wilson(wins,n),"distinct_stocks":len({x["code"] for x in group}),
                "both_hit_losses":sum(x["result"]=="SAME_BAR_AMBIGUOUS_LOSS" for x in group)}
        summary[kind]={"label":label,"samples":subsets}
    meta={"cutoff":cutoff,"generated_hkt":datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(),
          "coverage_universe":len(stocks),"eligible_stock_days":bottom_days,"exclusions":dict(skipped),
          "definition":"Next Hong Kong session open; +5% hit strictly before -8% within 20 trading days, both same day loss, unresolved loss",
          "not_included":"broker fees, stamp tax, slippage, FX, stop execution gaps",
          "time_split":"2026 Jan-Jun development, 2026 Jul onward holdout with 20 fully observed future sessions",
          "limitations":"143-stock universe fixed Sep 2026 (survivorship); possible corporate action issues; correlated trades; selected rule thresholds; no independent out-of-sample years"}
    data={"meta":meta,"patterns":summary,"events":dict(events)}
    out=ROOT/"research";out.mkdir(exist_ok=True)
    (out/"bottom_study.json").write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    lines=["# 港股143只低位结构初步回测","","数据截止："+cutoff,
      "","**重要：该历史统计不等于未来反弹概率。** 存在2026年9月固定股票池幸存者偏差。","",
      "条件：120交易日下跌至少20%，同时在2025全年或2026截至信号日的低位20%区间、距最低价≤15%。",
      "信号日收盘识别，下一交易日开盘入场，未来20日先到+5%且没先到-8%才算成功；同日双触发及未达标均视为失败。未扣交易费用。",
      "","| 形态 | 2026上半年成功/样本 | 上半年成功率 | 2026下半年成熟样本成功/样本 | 后段成功率 | 后段95%区间 |",
      "|---|---:|---:|---:|---:|---:|"]
    for kind,v in summary.items():
        a=v["samples"]["development"];b=v["samples"]["holdout"]
        pct=lambda x:"—" if x is None else str(x)+"%"
        lines.append(f"| {v['label']} | {a['wins']}/{a['n']} | {pct(a['rate_pct'])} | {b['wins']}/{b['n']} | {pct(b['rate_pct'])} | {pct(b['wilson95_pct'][0])}—{pct(b['wilson95_pct'][1])} |")
    lines += ["","### 客观定义",
       "新低：低于此前20交易日最低价且未收回；2B：低点距今5—20交易日，当日向下刺破不超过3%，随后收盘站回；",
       "二踩：最低价不低于前20日低点、距前低≤2%，当日收阳且收盘高于前日；强阳反转：创新低后收阳并收在当日价格范围上部30%；",
       "局部转强：收盘突破前三日最高价且近五日曾触及底部附近；2B确认：次日收盘突破2B当日最高价。",
       "","样本小于30或置信区间明显重叠的形态不能宣布更优；各个形态可交叉出现，样本彼此非独立。",
       "所有形态均为价格筛选信号，并非已确认支撑或确定的买入指令。"]
    (out/"bottom_study.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print("RESEARCH_SUMMARY "+json.dumps({"cutoff":cutoff,"patterns":summary,"base_days":bottom_days,"skipped":dict(skipped)},ensure_ascii=False))
if __name__=="__main__":main()
