# -*- coding: utf-8 -*-
"""只读探测 7：寻找真正可用的驱动因素。

背景：银行间 MTN 的 Δy 中位数今天 +7bp（说跌），但这 9 只 ETF 盘中全涨 0.3~0.67%。
      代理方向完全相反 → 不能用。必须另找驱动。

候选：
 D1. 国债期货 TS(2年) / TF(5年) / T(10年) —— 2 年久期对应 TS
 D2. 交易所债券收盘价（上交所固收平台正确接口）
 D3. 中债收益率曲线（chinabond 公开接口）
 D4. 已上市同类 ETF 的实时涨跌（511220 城投 / 511360 短融 / 511520 政金债）
 D5. 上交所 ETF 官方 IOPV（债券ETF 是否发布）
 D6. 银行间 Δy 的「成交时间」字段 —— 判断 bpNum 是否为隔夜累计
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


def _raw(url, referer=None, timeout=15, headers=None):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                timeout=timeout, context=_CTX)
    b = r.read()
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b


def _txt(url, referer=None, timeout=15):
    return _raw(url, referer, timeout).decode("utf-8", "ignore")


print("=" * 100)
print("D1  国债期货 TS/TF/T（2/5/10年）K线")
print("=" * 100)
import bond_sse_pcf_valuation as bsv
for sym in ("TS2612", "TF2612", "T2612", "TL2612", "TS0", "TF0", "T0"):
    kl = bsv.fetch_tl_kline(sym)
    print("  %-8s -> %d 根 %s" % (sym, len(kl), sorted(kl.items())[-3:] if kl else ""))

print()
print("=" * 100)
print("D2  上交所债券收盘价（多接口尝试）")
print("=" * 100)
sse_probes = [
    ("固收-现券成交",
     "http://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_GPLB_MCJS_GZLB_L&bondCode=115323"),
    ("债券行情-日",
     "http://query.sse.com.cn/marketdata/tradedata/queryTradeDataNew.do?productId=115323&reportType=DAY&tradeDate=20261117"),
    ("ETF行情",
     "http://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_FUND_ETF_HQ_L&fundCode=511070"),
    ("债券列表",
     "http://query.sse.com.cn/commonSoaQuery.do?sqlId=COMMON_SSE_ZQPZ_GPLB_MCJS_GZLB_L&bondCode=115323"),
    ("上证债券收盘",
     "http://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_ZQJYSJ_JYSJ_L&bondCode=115323"),
]
for nm, u in sse_probes:
    try:
        t = _txt(u, referer="http://www.sse.com.cn/")
        print("  %-16s -> %s" % (nm, t[:200].replace("\n", " ")))
    except Exception as e:
        print("  %-16s ❌ %s %s" % (nm, type(e).__name__, str(e)[:70]))

print()
print("=" * 100)
print("D3  中债收益率曲线（chinabond 公开接口）")
print("=" * 100)
cb = [
    ("国债收益率曲线",
     "https://yield.chinabond.com.cn/cbweb-mn/yc/searchYc?xyzSelect=txy&&workTime=2026-11-17&&dxbj=0&&qxll=0,1,3,5,7,10&&yqqxN=N&&yqqxK=K&&wrjxCBFlag=0&&locale=zh_CN"),
    ("中短期票据AAA",
     "https://yield.chinabond.com.cn/cbweb-mn/yc/searchYc?xyzSelect=zxpj&&workTime=2026-11-17&&dxbj=0&&qxll=0.5,1,3,5&&yqqxN=N&&yqqxK=K&&wrjxCBFlag=0&&locale=zh_CN"),
    ("企业债AAA",
     "https://yield.chinabond.com.cn/cbweb-mn/yc/searchYc?xyzSelect=qyz&&workTime=2026-11-17&&dxbj=0&&qxll=0.5,1,3,5&&yqqxN=N&&yqqxK=K&&wrjxCBFlag=0&&locale=zh_CN"),
]
for nm, u in cb:
    try:
        t = _txt(u, referer="https://yield.chinabond.com.cn/")
        print("  %-14s -> %s" % (nm, t[:260].replace("\n", " ")))
    except Exception as e:
        print("  %-14s ❌ %s %s" % (nm, type(e).__name__, str(e)[:70]))

print()
print("=" * 100)
print("D4  已上市同类 ETF 的实时涨跌（同业参照）")
print("=" * 100)
PEERS = {
    "511220": "城投债ETF", "511360": "短融ETF", "511520": "政金债ETF",
    "511090": "30年国债ETF", "511130": "30年国债ETF博时",
    "511380": "沪深300ETF(对照)", "159650": "科创债ETF?",
}
for c, nm in PEERS.items():
    pre = "sh" if c[0] == "5" else "sz"
    try:
        raw = _raw("https://hq.sinajs.cn/list=%s%s" % (pre, c),
                   referer="https://finance.sina.com.cn/")
        t = raw.decode("gb18030", "ignore")
        body = t.split('"')[1] if '"' in t else ""
        f = body.split(",")
        if len(f) > 5:
            pc, px = float(f[2]), float(f[3])
            print("  %-7s %-14s 昨收=%-8s 现价=%-8s 涨跌=%+.3f%%"
                  % (c, nm, pc, px, (px / pc - 1) * 100 if pc else 0))
        else:
            print("  %-7s %-14s 无数据" % (c, nm))
    except Exception as e:
        print("  %-7s %-14s ❌ %s" % (c, nm, str(e)[:60]))

print()
print("=" * 100)
print("D5  上交所 ETF 官方 IOPV（债券 ETF 是否发布）")
print("=" * 100)
for c in ("511070", "551900", "511220"):
    probes = [
        ("SSE IOPV", "http://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_FUND_ETF_IOPV&fundCode=%s" % c),
        ("东财 f78", "https://push2.eastmoney.com/api/qt/stock/get?secid=1.%s&fields=f43,f78,f57,f58,f60" % c),
    ]
    for nm, u in probes:
        try:
            t = _txt(u, referer="https://www.sse.com.cn/")
            print("  %-7s %-10s -> %s" % (c, nm, t[:180].replace("\n", " ")))
        except Exception as e:
            print("  %-7s %-10s ❌ %s %s" % (c, nm, type(e).__name__, str(e)[:60]))

print()
print("=" * 100)
print("D6  银行间 Δy 的成交时间字段（判断 bpNum 是否为隔夜累计）")
print("=" * 100)
import bond_pcf_valuation as bpv
spot = bpv.fetch_spot_deal()
rows = [(k, v) for k, v in spot.items() if "MTN" in k][:8]
for k, v in rows:
    print("  %-28s %s" % (k, json.dumps(v, ensure_ascii=False)))
