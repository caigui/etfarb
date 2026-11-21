# -*- coding: utf-8 -*-
"""只读探测 12（最后）：信用债收益率曲线免费源。

单因子（政金债Δy）解释不了这批 ETF 的净值波动 —— 回归出的隐含久期 8~13 年，
对 3~5 年公司债物理上不可能，差异就是**信用利差走阔**。
要算准必须拿到「AAA 公司债/中票 收益率曲线」。

候选：
 E1. 中国货币网（已验证可用）的收益率曲线接口
 E2. 中债登 chinabond 收益率曲线（POST 方式）
 E3. 中证指数真实 API 路径
 E4. 上交所债券收盘价（找正确 sqlId）
"""
import io, sys, os, json, re, ssl, urllib.request, urllib.parse, gzip
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def req(url, data=None, referer=None, timeout=15, hdr=None):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    if hdr:
        h.update(hdr)
    if data is not None:
        if isinstance(data, dict):
            data = urllib.parse.urlencode(data).encode()
        r = urllib.request.Request(url, data=data, headers=h)
    else:
        r = urllib.request.Request(url, headers=h)
    b = urllib.request.urlopen(r, timeout=timeout, context=_CTX).read()
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b.decode("utf-8", "ignore")


print("=" * 100)
print("E1  中国货币网 收益率曲线接口（该域名已验证可用）")
print("=" * 100)
CM_REF = "https://www.chinamoney.com.cn/chinese/bkcurvfx/"
cands = [
    ("ClsYldCurv 列表",
     "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/ClsYldCurv?lang=CN&flag=1"),
    ("CbtYldCurv",
     "https://www.chinamoney.com.cn/ags/ms/cm-u-md-bond/CbtYldCurv?lang=CN&flag=1"),
    ("BondYldCurv",
     "https://www.chinamoney.com.cn/ags/ms/cm-u-bond-market/BondYldCurv?lang=CN"),
    ("curv 查询(post flag=1)",
     "https://www.chinamoney.com.cn/ags/ms/cm-u-md-bond/CbtYldCurv"),
    ("收益曲线页",
     "https://www.chinamoney.com.cn/chinese/bkcurvfx/"),
]
for nm, u in cands:
    for method in ("get", "post"):
        try:
            if method == "post":
                t = req(u, data={"lang": "CN", "flag": "1"}, referer=CM_REF,
                        hdr={"X-Requested-With": "XMLHttpRequest"})
            else:
                t = req(u, referer=CM_REF)
            flag = "✅" if len(t) < 3000 else "（HTML页）"
            print("  %-18s %-4s %s len=%-6d %s" % (nm, method.upper(), flag, len(t),
                                                   t[:200].replace("\n", " ")))
        except Exception as e:
            print("  %-18s %-4s ❌ %s %s" % (nm, method.upper(), type(e).__name__, str(e)[:60]))

print()
print("=" * 100)
print("E2  中债登 chinabond（POST）")
print("=" * 100)
CB = "https://yield.chinabond.com.cn/cbweb-mn/yc/searchYc"
for nm, payload in [
    ("国债", {"xyzSelect": "txy", "workTime": "2026-11-17", "dxbj": "0",
              "qxll": "0,1,3,5,7,10", "yqqxN": "N", "yqqxK": "K",
              "wrjxCBFlag": "0", "locale": "zh_CN"}),
    ("中短期票据AAA", {"xyzSelect": "zxpj", "workTime": "2026-11-17", "dxbj": "0",
                       "qxll": "0.5,1,3,5", "yqqxN": "N", "yqqxK": "K",
                       "wrjxCBFlag": "0", "locale": "zh_CN"}),
]:
    try:
        t = req(CB, data=payload, referer="https://yield.chinabond.com.cn/")
        print("  %-14s -> %s" % (nm, t[:300].replace("\n", " ")))
    except Exception as e:
        print("  %-14s ❌ %s %s" % (nm, type(e).__name__, str(e)[:80]))

print()
print("=" * 100)
print("E3  中证指数真实 API 路径")
print("=" * 100)
cs = [
    ("index-hq", "https://www.csindex.com.cn/csindex-home/index/index-hq?indexCode=950245"),
    ("perf 带参", "https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=950245&startDate=20261101&endDate=20261117"),
    ("indexInfo", "https://www.csindex.com.cn/csindex-home/indexInfo/index-basic-info?indexCode=950245"),
    ("v2 perf", "https://www.csindex.com.cn/csindex-home/perf/index-perf/v2?indexCode=950245"),
    ("indices list", "https://www.csindex.com.cn/csindex-home/index/list?pageNum=1&pageSize=10&searchInput=950245"),
]
for nm, u in cs:
    try:
        t = req(u, referer="https://www.csindex.com.cn/")
        print("  %-14s -> %s" % (nm, t[:220].replace("\n", " ")))
    except Exception as e:
        print("  %-14s ❌ %s %s" % (nm, type(e).__name__, str(e)[:80]))

print()
print("=" * 100)
print("E4  上交所债券行情（试探正确 sqlId）")
print("=" * 100)
sse = [
    ("债券成交概览", "http://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_ZQJYSJ_JYSJ_L&bondCode=115323&isPagination=false"),
    ("固收现券", "http://query.sse.com.cn/commonSoaQuery.do?sqlId=COMMON_SSE_ZQPZ_GZLB_L&bondCode=115323"),
    ("ETF清单", "http://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_FUND_ETF_LIST_L&fundCode=511070&isPagination=false"),
    ("债券日行情", "http://query.sse.com.cn/marketdata/tradedata/queryTradeDataNew.do?productId=115323&reportType=DAY&beginDate=20261110&endDate=20261117"),
]
for nm, u in sse:
    try:
        t = req(u, referer="http://www.sse.com.cn/")
        print("  %-14s -> %s" % (nm, t[:220].replace("\n", " ")))
    except Exception as e:
        print("  %-14s ❌ %s %s" % (nm, type(e).__name__, str(e)[:80]))
