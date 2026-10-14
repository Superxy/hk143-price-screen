# -*- coding: utf-8 -*-
"""HK143 exit comparison: 2025 history, 2026 2B/retest signals; OHLC only.

Same next-session triggered entry and structural loss cap for 2R, 3R and
prior resistance. All levels are backtest hypotheses, not future predictions.
"""
from __future__ import annotations
import argparse, csv, json, math
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean,median
from zoneinfo import ZoneInfo
import exchange_calendars as xcals
import pandas as pd

STRATEGIES=("2R","3R","PREVIOUS_RESISTANCE")
LABELS={"2R":"固定2R止盈","3R":"固定3R止盈","PREVIOUS_RESISTANCE":"前期阻力止盈"}

def price_bars(root,code,end):
    out={}
    for year in range(2025,int(end[:4])+1):
        file=root/"data"/str(year)/f"{code}_{year}.csv"
        if not file.exists():continue
        df=pd.read_csv(file,dtype={"date":str},encoding="utf-8-sig")
        if not {"date","open","high","low","close"}.issubset(df.columns):continue
        for row in df.itertuples(index=False):
            day=str(row.date)[:10]
            if not "2025-01-01"<=day<=end:continue
            try: op,hi,lo,cl=map(float,(row.open,row.high,row.low,row.close))
            except (ValueError,TypeError):continue
            if all(math.isfinite(v) and v>0 for v in (op,hi,lo,cl)) and hi>=max(op,lo,cl) and lo<=min(op,hi,cl):
                out[day]={"open":op,"high":hi,"low":lo,"close":cl}
    return out

def low_zone(p,l,h):
    return h>l>0 and (p-l)/(h-l)<=.20 and p/l-1<=.15

def find_entries(code,name,bars,dates,count):
    y25=[d for d in dates if d.startswith("2025")]
    prev=[bars[d] for d in y25 if d in bars]
    prev_full=len(prev)==len(y25)
    lo25=min((r["low"] for r in prev),default=None)
    hi25=max((r["high"] for r in prev),default=None)
    lo26,hi26=math.inf,-math.inf
    current_full=True
    last_entry_signal=-999
    entries=[]
    for i,day in enumerate(dates):
        if not day.startswith("2026"):continue
        r=bars.get(day)
        if r is None:current_full=False;continue
        lo26=min(lo26,r["low"]);hi26=max(hi26,r["high"])
        if i<120 or not current_full:continue
        if not ((prev_full and low_zone(r["close"],lo25,hi25)) or low_zone(r["close"],lo26,hi26)):continue
        earlier=bars.get(dates[i-120])
        if earlier is None or r["close"]/earlier["close"]-1>-.20:continue
        last20=dates[i-20:i]
        if len(last20)!=20 or any(x not in bars for x in last20):
            count["missing_lookback"]+=1;continue
        if any(abs(bars[dates[j]]["open"]/bars[dates[j-1]]["close"]-1)>.45
               for j in range(i-119,i+1) if dates[j] in bars and dates[j-1] in bars):
            count["suspected_corporate_action"]+=1;continue
        lows=[bars[x]["low"] for x in last20]
        low=min(lows);low_pos=lows.index(low);age=20-low_pos
        if not 5<=age<=20:continue
        two_b=low*.97<=r["low"]<low and r["close"]>low
        retest=(low<=r["low"]<=low*1.02 and r["close"]>r["open"] and
                r["close"]>bars[last20[-1]]["close"])
        if not (two_b or retest):continue
        count["signals_matching_filters"]+=1
        if i-last_entry_signal<=20:
            count["same_stock_20session_duplicates"]+=1;continue
        if i+20>=len(dates):
            count["immature_20session_window"]+=1;continue
        forward=dates[i+1:i+21]
        if any(x not in bars for x in forward):
            count["incomplete_future_bars"]+=1;continue
        nextbar=bars[forward[0]]
        trigger=r["high"]*1.002
        stop=(r["low"] if two_b else low)*.995
        if nextbar["open"]<=stop:
            count["invalidated_before_open_entry"]+=1;continue
        if nextbar["high"]<trigger:
            count["no_breakout_next_day"]+=1;continue
        entry=max(trigger,nextbar["open"])
        if entry<=stop:
            count["invalid_entry_stop"]+=1;continue
        if (entry-stop)/entry>.08+1e-12:
            count["risk_exceeds_8pct"]+=1;continue
        # Highs strictly between original reference low and today's signal.
        after_low=last20[low_pos+1:]
        resistance=max((bars[d]["high"] for d in after_low),default=None)
        if resistance is None or resistance<=entry:
            resistance=None
            count["no_resistance_above_entry"]+=1
        last_entry_signal=i
        entries.append({"code":code,"name":name,"kind":"2B" if two_b else "RETEST",
                        "signal_day":day,"entry_day":forward[0],"entry":entry,
                        "stop":stop,"resistance":resistance,"future":forward})
    return entries

def simulate(t,bars,strategy,fee):
    entry,stop=t["entry"],t["stop"]
    target={"2R":entry+2*(entry-stop),"3R":entry+3*(entry-stop),
            "PREVIOUS_RESISTANCE":t["resistance"]}[strategy]
    if target is None or target<=entry:return None
    reason=None;exit_price=None;exit_day=None;both=False
    for j,day in enumerate(t["future"]):
        b=bars[day]
        if j==0:
            # Same-day path is unknown; assume stop occurs after the stop-entry.
            if b["low"]<=stop:
                both=b["high"]>=target
                reason="ambiguous_stop_first" if both else "stop"
                exit_price=stop;exit_day=day;break
            if b["high"]>=target:
                reason="target";exit_price=target;exit_day=day;break
            continue
        if b["open"]<=stop:
            reason="stop_gap";exit_price=b["open"];exit_day=day;break
        if b["open"]>=target:
            reason="target_gap";exit_price=b["open"];exit_day=day;break
        if b["low"]<=stop:
            both=b["high"]>=target
            reason="ambiguous_stop_first" if both else "stop"
            exit_price=stop;exit_day=day;break
        if b["high"]>=target:
            reason="target";exit_price=target;exit_day=day;break
    if reason is None:
        reason="time_exit_20d";exit_day=t["future"][-1]
        exit_price=bars[exit_day]["close"]
    gross=100*(exit_price/entry-1)
    net=100*(exit_price*(1-fee)-entry*(1+fee))/(entry*(1+fee))
    return {"code":t["code"],"name":t["name"],"kind":t["kind"],
            "signal":t["signal_day"],"entry_date":t["entry_day"],
            "exit_date":exit_day,"method":strategy,"entry":round(entry,6),
            "stop":round(stop,6),"target":round(target,6),
            "exit":round(exit_price,6),"reason":reason,
            "gross_pct":round(gross,5),"net_pct":round(net,5),
            "net_r":round((net/100)/((entry-stop)/entry),5),
            "ambiguous":both,"resistance":t["resistance"]}

def calc(rows):
    n=len(rows)
    if not n:return {"n":0,"win_rate_pct":None,"mean_net_pct":None,"median_net_pct":None,
                     "mean_net_r":None,"profit_factor":None,"target_hit_pct":None,
                     "stop_hit_pct":None,"time_exit_pct":None,"ambiguous":0,"stocks":0}
    net=[x["net_pct"] for x in rows]
    plus=sum(v for v in net if v>0);minus=-sum(v for v in net if v<0)
    return {"n":n,"win_rate_pct":round(100*sum(v>0 for v in net)/n,1),
            "mean_net_pct":round(mean(net),3),"median_net_pct":round(median(net),3),
            "mean_net_r":round(mean(x["net_r"] for x in rows),3),
            "profit_factor":round(plus/minus,3) if minus else None,
            "target_hit_pct":round(100*sum(x["reason"].startswith("target") for x in rows)/n,1),
            "stop_hit_pct":round(100*sum(x["reason"].startswith("stop") or
                x["reason"]=="ambiguous_stop_first" for x in rows)/n,1),
            "time_exit_pct":round(100*sum(x["reason"]=="time_exit_20d" for x in rows)/n,1),
            "ambiguous":sum(x["ambiguous"] for x in rows),
            "stocks":len({x["code"] for x in rows})}

def make_md(result):
    m=result["meta"];count=m["counts"]
    def pct(x):return "—" if x is None else f"{x:.2f}%"
    def row(label,a):
        return f"| {label} | {a['n']} | {pct(a['win_rate_pct'])} | {pct(a['mean_net_pct'])} | {pct(a['median_net_pct'])} | {a['mean_net_r'] if a['mean_net_r'] is not None else '—'} | {pct(a['target_hit_pct'])} | {pct(a['stop_hit_pct'])} | {pct(a['time_exit_pct'])} |"
    lines=["# 港股143只：2R、3R、历史阻力三种止盈的真实历史回测","",
        f"- 数据范围：2025年1月1日至{m['cutoff']}；2025年作为120交易日和上一年度低位的回看基准，2026年产生入场信号。",
        "- 信号只在当日及之前数据上计算。143只股票来自2026年固定名单，存在事后选择偏差。",
        "- 入场：信号日最高价×1.002，只有次日最高价触及才模拟入场；若次日跳空高开则按开盘价，重算8%限制。",
        "- 止损：2B假跌破当日最低价×0.995；二次回踩前低×0.995。",
        "- 出场：持有最多20个香港交易日，期间先到止盈/止损先出场，同日同时触发按止损优先；止损向下跳空按开盘价；到期按收盘价。",
        "- 历史阻力：前低形成后、当前结构信号发生前最高价；若阻力不在入场价上方，则无法作为止盈方案，需分开报告样本数。",
        f"- 费用假设：买、卖各{m['per_side_cost_pct']}%，包含粗略交易费用及摩擦，总往返约{2*m['per_side_cost_pct']:.2f}%；不是准确的港股通费单。",
        "- 每笔交易等权统计，平均净收益不是组合总收益，不保证可在模拟价格成交。","",
        "## 资格、执行与缺失统计",""]
    for k,v in count.items():lines.append(f"- {k}: {v}")
    lines += ["","## A. 2R 与 3R：全部已触发并满足8%价格风险的交易","",
              "| 出场方案 | 笔数 | 净盈利比例 | 平均净收益 | 净收益中位数 | 平均净R | 触及止盈 | 触及止损 | 满20日退出 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for key in ("2R","3R"):lines.append(row(LABELS[key],result["all"][key]))
    lines += ["","## B. 三种方法公平对比：仅针对有正向历史阻力的共同样本","",
              "| 出场方案 | 笔数 | 净盈利比例 | 平均净收益 | 净收益中位数 | 平均净R | 触及止盈 | 触及止损 | 满20日退出 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for key in STRATEGIES:lines.append(row(LABELS[key],result["common"][key]))
    lines += ["","## C. 共同样本：2B 与 二踩分别比较",""]
    for typ in ("2B","RETEST"):
        lines += [f"### {typ}", "", "| 出场方案 | 笔数 | 净胜率 | 平均净收益 | 平均净R |","|---|---:|---:|---:|---:|"]
        for key in STRATEGIES:
            d=result["by_signal"][typ][key]
            lines.append(f"| {LABELS[key]} | {d['n']} | {pct(d['win_rate_pct'])} | {pct(d['mean_net_pct'])} | {d['mean_net_r'] if d['mean_net_r'] is not None else '—'} |")
        lines.append("")
    lines += ["## 限制与偏差","",
        "1. 2025年无2024全年数据，故无法按相同年度筛选规则回测2025年的信号。只对2026年信号做符合原系统定义的前测重放。",
        "2. 2026年10月后20交易日结果尚未知，末尾未成熟样本不计胜败；同一股票20交易日内的重复信号去重，样本可能很小。",
        "3. 用日OHLC无法知道日内触价的真实先后顺序，同日双触按止损优先；触发当天可能先打到止损再触发买入，结果偏保守。",
        "4. 股票池为事后确定的143只，存在市场共同涨跌和生存偏差，样本不是独立随机变量。",
        "5. 尚未考虑港股最低报价单位、股数手数、券商最低费用、印花税逐笔舍入、止损流动性和换汇。",
        "6. 三种止盈统计不能被解释为未来收益承诺、已验证的最优参数或直接买卖指令。",""]
    return "\n".join(lines)

def run(root,fee_side=.0015):
    if not 0<=fee_side<=.02:raise ValueError("不合法的费用参数")
    latest=json.loads((root/"data"/"latest.json").read_text(encoding="utf-8"))
    cutoff=latest["meta"]["target_session_hkt"]
    if cutoff[:4]!="2026":raise ValueError("本版研究只允许2026信号；跨年须更新以免错误回测")
    cal=xcals.get_calendar("XHKG")
    dates=[d.date().isoformat() for d in cal.sessions_in_range(pd.Timestamp("2025-01-01"),pd.Timestamp(cutoff))]
    with (root/"config"/"stocks.csv").open(encoding="utf-8-sig",newline="") as f:
        stocks=[(r["code"].zfill(5),r["name"]) for r in csv.DictReader(f)]
    if len(stocks)!=143 or len({a for a,_ in stocks})!=143:raise ValueError("143只股票清单不完整")
    count=Counter();entries=[];events=[]
    for code,name in stocks:
        bars=price_bars(root,code,cutoff)
        if not bars:count["missing_price_file_stock"]+=1;continue
        items=find_entries(code,name,bars,dates,count)
        entries.extend(items)
        for t in items:
            for method in STRATEGIES:
                ev=simulate(t,bars,method,fee_side)
                if ev is not None:events.append(ev)
    count["actual_entry_with_max_8pct_risk"]=len(entries)
    count["entries_with_positive_resistance"]=sum(x["resistance"] is not None for x in entries)
    common={(x["code"],x["signal_day"]) for x in entries if x["resistance"] is not None}
    ok=lambda r:(r["code"],r["signal"]) in common
    all_stats={k:calc([r for r in events if r["method"]==k]) for k in ("2R","3R")}
    common_stats={k:calc([r for r in events if r["method"]==k and ok(r)]) for k in STRATEGIES}
    by_kind={kind:{k:calc([r for r in events if r["method"]==k and ok(r) and r["kind"]==kind]) for k in STRATEGIES} for kind in ("2B","RETEST")}
    result={"meta":{"cutoff":cutoff,"generated_hkt":datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(),
                    "universe":143,"per_side_cost_pct":100*fee_side,"counts":dict(count),
                    "version":"exit_comparison_v1","source":"Sina/AKShare unadjusted daily OHLC, HK143 GitHub"},
            "all":all_stats,"common":common_stats,"by_signal":by_kind,"events":events}
    path=root/"research";path.mkdir(parents=True,exist_ok=True)
    (path/"exit_comparison.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (path/"exit_comparison.md").write_text(make_md(result),encoding="utf-8")
    fields=["code","name","kind","signal","entry_date","exit_date","method","entry","stop","target","exit","reason","gross_pct","net_pct","net_r","ambiguous","resistance"]
    with (path/"exit_comparison_trades.csv").open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for item in events:w.writerow(item)
    print("EXIT_COMPARISON "+json.dumps({"cutoff":cutoff,"counts":dict(count),"all":all_stats,"common":common_stats,"by_signal":by_kind},ensure_ascii=False),flush=True)
    return result

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument("--fee-side",type=float,default=.0015)
    args=parser.parse_args()
    run(args.output.resolve(),fee_side=args.fee_side)
