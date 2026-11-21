# -*- coding: utf-8 -*-
"""只读探测 13（突破）：中证指数官网 perf 接口能否给出 9 只标的历史指数点位。

perf 接口已验证：
  GET https://www.csindex.com.cn/csindex-home/perf/index-perf
      ?indexCode=950245&startDate=20261101&endDate=20261117
  → {"code":"200","data":[{"tradeDate":"20261101","indexCode":"950245",
     "indexNameCnAll":"上证基准做市公司债指数",...,"close":...,"change":...}]}

要确认：
 1. 返回字段（close / change / 是否有涨跌幅）
 2. 各 ETF 的真实跟踪指数代码（950245 已知；AAA科创债待查）
 3. 指数日涨跌 vs ETF 净值涨跌 的一致性（这才是正确驱动）
"""
import io, sys, os, json, re, ssl, urllib.request, gzip
from datetime import date, timedelta
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def req(url, referer=None, timeout=20):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    b = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                               timeout=timeout, context=_CTX).read()
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b.decode("utf-8", "ignore")


def perf(index_code, start, end):
    u = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
         "?indexCode=%s&startDate=%s&endDate=%s" % (index_code, start, end))
    t = req(u, referer="https://www.csindex.com.cn/")
    return json.loads(t)


START = (date.today() - timedelta(days=45)).strftime("%Y%m%d")
END = date.today().strftime("%Y%m%d")

print("=" * 100)
print("A  950245 沪做市公司债 原始返回（完整字段）")
print("=" * 100)
try:
    j = perf("950245", START, END)
    d = j.get("data") or []
    print("  code=%s 记录数=%d" % (j.get("code"), len(d)))
    if d:
        print("  首条完整字段：")
        print("   " + json.dumps(d[0], ensure_ascii=False, indent=2).replace("\n", "\n   "))
        print("  最近 5 条：")
        for r in d[-5:]:
            print("   ", json.dumps(r, ensure_ascii=False))
except Exception as e:
    print("  ❌", type(e).__name__, str(e)[:150])

print()
print("=" * 100)
print("B  各 ETF 的跟踪指数代码探测")
print("=" * 100)
# 已知/候选指数代码
CAND = {
    "950245": "上证基准做市公司债指数（511070/511110/511190/511200）",
    "932158": "候选·中证AAA科创债",
    "932063": "候选·中证AAA科创债2",
    "950050": "候选·上证AAA科创债",
    "950051": "候选·上证AAA科创债2",
    "932159": "候选",
    "931920": "候选",
    "932081": "候选",
    "950246": "候选",
    "H11001": "中证全债(对照)",
}
for c, nm in CAND.items():
    try:
        j = perf(c, START, END)
        d = j.get("data") or []
        if d:
            names = {r.get("indexNameCnAll") or r.get("indexNameCn") for r in d}
            print("  %-8s ✅ %d 条  名称=%s  最新=%s close=%s"
                  % (c, len(d), "/".join(str(x) for x in names if x),
                     d[-1].get("tradeDate"), d[-1].get("close")))
        else:
            print("  %-8s ❌ 空（data=[]）  %s" % (c, nm))
    except Exception as e:
        print("  %-8s ❌ %s %s" % (c, type(e).__name__, str(e)[:60]))
