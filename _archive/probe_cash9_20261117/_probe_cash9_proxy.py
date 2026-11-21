# -*- coding: utf-8 -*-
"""只读探测 3：代理池质量 + 曲线源 + 159600 深市通道。

Q1. 银行间 MTN（中票）样本的期限/收益率分布 —— 能否按期限分桶代理？
Q2. 中债/中证 AAA 公司债收益率曲线有无免费源？
Q3. 159600 深市 PCF 其他通道
Q4. 用历史净值反推「隐含组合久期」，检验代理模型是否稳定
"""
import io, sys, os, json, re, ssl, urllib.request, traceback
from datetime import date, timedelta
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _txt(url, referer=None, timeout=12):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    return urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                  timeout=timeout, context=_CTX).read().decode("utf-8", "ignore")


import bond_pcf_valuation as bpv
import bond_sse_pcf_valuation as bsv

print("=" * 100)
print("Q1  银行间信用债样本分布（MTN / 二级资本债 / 科创债）")
print("=" * 100)
spot = bpv.fetch_spot_deal()
print("总条数 %d  行情日 %s\n" % (len(spot), bpv._latest_spot_date(spot)))


def bucket(kw, label):
    rows = [(k, v) for k, v in spot.items() if kw in k and v.get("dy_bp") is not None]
    if not rows:
        print("  %-14s 无样本" % label)
        return []
    terms = sorted(x[1].get("term") or 0 for x in rows)
    ylds = sorted(x[1].get("yield") or 0 for x in rows)
    dys = sorted(x[1]["dy_bp"] for x in rows)
    n = len(rows)
    print("  %-14s n=%-3d 期限中位=%.2f [%.2f~%.2f]  收益率中位=%.3f [%.2f~%.2f]  Δy中位=%+.2f [%+.2f~%+.2f]"
          % (label, n, terms[n // 2], terms[0], terms[-1],
             ylds[n // 2], ylds[0], ylds[-1],
             dys[n // 2], dys[0], dys[-1]))
    return rows


mtn = bucket("MTN", "中票MTN")
bucket("二级资本债", "二级资本债")
bucket("科创债", "科创债")
bucket("城投", "城投")
bucket("PPN", "PPN")

print("\n  中票样本按期限分桶：")
if mtn:
    bl = {"<=1y": [], "1-3y": [], "3-5y": [], "5-7y": [], ">7y": []}
    for k, v in mtn:
        t = v.get("term") or 0
        key = ("<=1y" if t <= 1 else "1-3y" if t <= 3 else "3-5y" if t <= 5
               else "5-7y" if t <= 7 else ">7y")
        bl[key].append(v["dy_bp"])
    for k, xs in bl.items():
        if xs:
            xs.sort()
            print("    %-6s n=%-3d Δy中位=%+.2f  均值=%+.2f"
                  % (k, len(xs), xs[len(xs) // 2], sum(xs) / len(xs)))

print("\n  中票样本 前 12 只明细：")
for k, v in list((x for x in spot.items() if "MTN" in x[0] and x[1].get("dy_bp") is not None))[:12]:
    print("    %-30s clean=%-9s yld=%-7s Δy=%+7.2f term=%s"
          % (k, v.get("clean"), v.get("yield"), v["dy_bp"], v.get("term")))

print()
print("=" * 100)
print("Q2  中债 / 中证 收益率曲线免费源")
print("=" * 100)
probes = [
    ("中证指数 官网行情接口",
     "https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=950245"),
    ("中证指数 日行情",
     "https://www.csindex.com.cn/csindex-home/indexInfo/index-daily-perf?indexCode=950245"),
    ("中债 国债收益率曲线(试)",
     "https://yield.chinabond.com.cn/cbweb-mn/yield_main"),
    ("东财 中证AAA科创债 搜索",
     "https://searchapi.eastmoney.com/api/suggest/get?input=AAA%E7%A7%91%E5%88%9B%E5%80%BA&type=14&token=D43BF722C8E33BDC906FB84D85E326E8"),
]
for nm, u in probes:
    try:
        t = _txt(u, referer="https://www.csindex.com.cn/")
        print("  %-26s -> %s" % (nm, t[:240].replace("\n", " ")))
    except Exception as e:
        print("  %-26s ❌ %s %s" % (nm, type(e).__name__, str(e)[:90]))

print()
print("=" * 100)
print("Q3  159600 深市 PCF 通道")
print("=" * 100)
sz = [
    ("深交所 ETF 申购赎回清单 CATALOGID=1945_pcf",
     "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1945_pcf&txtQueryKeyAndJC=159600&TABKEY=tab1"),
    ("深交所 ETF 清单 CATALOGID=1945_etf",
     "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1945_etf&txtQueryKeyAndJC=159600&TABKEY=tab1"),
    ("嘉实基金 PCF(试 /upload)",
     "https://www.jsfund.cn/upload/pcf/159600.xml"),
    ("嘉实基金 产品页",
     "https://www.jsfund.cn/main/fund/159600.shtml"),
]
for nm, u in sz:
    try:
        t = _txt(u, referer="https://www.szse.cn/")
        print("  %-40s -> %s" % (nm, t[:220].replace("\n", " ")))
    except Exception as e:
        print("  %-40s ❌ %s %s" % (nm, type(e).__name__, str(e)[:90]))

print()
print("=" * 100)
print("Q4  用历史净值反推隐含组合久期（检验代理模型是否稳定）")
print("=" * 100)
CODES = ["511070", "511110", "511190", "511200", "551900", "551030", "551500", "551550", "159600"]
for c in CODES:
    try:
        h = bsv._nav_history(c, days=20)
        if len(h) < 5:
            print("  %s 净值不足" % c)
            continue
        h = list(reversed(h))          # 升序
        chgs = []
        for i in range(1, len(h)):
            r = (h[i][1] / h[i - 1][1] - 1) * 100
            chgs.append(r)
        print("  %-7s n=%2d  日均变动 %+.4f%%  |日变动|中位 %.4f%%  最大单日 %.4f%%"
              % (c, len(chgs), sum(chgs) / len(chgs),
                 sorted(abs(x) for x in chgs)[len(chgs) // 2],
                 max(abs(x) for x in chgs)))
        print("        明细: %s" % ", ".join("%s%+.3f" % (d[5:], (h[i][1] / h[i - 1][1] - 1) * 100)
                                          for i, d in enumerate(h[1:], 1)))
    except Exception as e:
        print("  %-7s ❌ %s %s" % (c, type(e).__name__, str(e)[:80]))
