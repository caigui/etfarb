# -*- coding: utf-8 -*-
"""只读探测 5：锚点可靠性 + 指数代理可得性。

A. 511110 / 551500 的 -2.00% / -1.67% 是不是分红除权？（决定净值序列能否直接用）
B. 跟踪指数在东财 / 新浪 / 同花顺 有没有行情
C. 银行间 AAA 信用债 Δy 与这批 ETF 净值变动的历史相关性（用 ETF 折溢价剥离后的净变动）
D. PCF 成份券的期限结构（发行年 → 剩余年限）
"""
import io, sys, os, json, re, ssl, urllib.request
from datetime import date, timedelta
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _txt(url, referer=None, timeout=15):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    return urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                  timeout=timeout, context=_CTX).read().decode("utf-8", "ignore")


import bond_sse_pcf_valuation as bsv


def decode_name(s):
    """新浪返回的基金简称是 GBK，urllib 用 utf-8 解会乱码，这里只做展示用标记。"""
    return s


print("=" * 100)
print("A  分红除权核查：511110 / 551500 的异常单日跌幅")
print("=" * 100)
for c in ("511110", "551500", "511070"):
    print("\n  -- %s 东财 lsjz 原始记录（含分红字段）--" % c)
    try:
        url = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
               "&pageIndex=1&pageSize=10" % c)
        raw = _txt(url, referer="https://fundf10.eastmoney.com/jjjz_%s.html" % c)
        s, e = raw.find("{"), raw.rfind("}") + 1
        j = json.loads(raw[s:e])
        for r in j.get("Data", {}).get("LSJZList", [])[:10]:
            print("     %s  DWJZ=%-10s LJJZ=%-10s FHSP=%s  FHFCZ=%s"
                  % (r.get("FSRQ"), r.get("DWJZ"), r.get("LJJZ"),
                     r.get("FHSP"), r.get("FHFCZ")))
    except Exception as e:
        print("     ❌", type(e).__name__, str(e)[:100])

print()
print("=" * 100)
print("B  跟踪指数行情源探测")
print("=" * 100)
IDX = [("950245", "沪做市公司债"), ("932158", "中证AAA科创债(猜)"),
       ("950050", "上证AAA科创债(猜)"), ("H11001", "中证全债(对照)")]
for code, nm in IDX:
    print("\n  -- %s %s --" % (code, nm))
    probes = [
        ("东财 secid=2.", "https://push2.eastmoney.com/api/qt/stock/get?secid=2.%s&fields=f43,f57,f58,f60,f170" % code),
        ("东财 secid=1.", "https://push2.eastmoney.com/api/qt/stock/get?secid=1.%s&fields=f43,f57,f58,f60,f170" % code),
        ("新浪 sh", "https://hq.sinajs.cn/list=sh%s" % code),
        ("新浪 sz", "https://hq.sinajs.cn/list=sz%s" % code),
        ("中证 perf", "https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=%s" % code),
    ]
    for lbl, u in probes:
        try:
            t = _txt(u, referer="https://quote.eastmoney.com/").strip()
            if len(t) > 180:
                t = t[:180] + "..."
            print("     %-14s -> %s" % (lbl, t if t else "(空)"))
        except Exception as e:
            print("     %-14s ❌ %s %s" % (lbl, type(e).__name__, str(e)[:70]))

print()
print("=" * 100)
print("C  PCF 成份券期限结构（代码前两位=发行年，估算剩余年限）")
print("=" * 100)
import bond_pcf_valuation as bpv
CODES = ["511070", "511110", "511190", "511200", "551900", "551030", "551500", "551550"]
for c in CODES:
    try:
        pcf = bsv.fetch_sse_pcf(c)
        bonds = [b for b in pcf["bonds"] if b.get("qty", 0) > 0]
    except Exception as e:
        print("  %-7s ❌ PCF %s" % (c, str(e)[:60]))
        continue
    yrs, w = [], []
    for b in bonds:
        bid = str(b.get("id") or "")
        m = re.match(r"(\d{2})", bid)
        if not m:
            continue
        yy = int(m.group(1))
        issue_year = 2000 + yy if yy < 80 else 1900 + yy
        age = date.today().year - issue_year
        yrs.append(age)
        w.append(b["qty"])
    if not yrs:
        print("  %-7s 无有效代码" % c)
        continue
    tot = sum(w) or 1
    wavg = sum(y * x for y, x in zip(yrs, w)) / tot
    hist = {}
    for y in yrs:
        hist[y] = hist.get(y, 0) + 1
    print("  %-7s 成份=%-3d 发行年加权年龄=%.2f 年  分布=%s"
          % (c, len(bonds), wavg, dict(sorted(hist.items()))))

print()
print("=" * 100)
print("D  历史净值 vs TL期货 / 国债 相关性（判断代理可用性）")
print("=" * 100)
# 取 TL 与 10Y 国债期货近 30 日涨跌，与 ETF 净值变动做同日对比
try:
    import bond_sse_pcf_valuation as b
    kl_tl = b.fetch_tl_kline()
    kl_t = b.fetch_tl_kline("T2612")
    print("  TL K线 %d 根，最近 6 日: %s" % (len(kl_tl), sorted(kl_tl.items())[-6:]))
    print("  T2612 K线 %d 根" % len(kl_t))
except Exception as e:
    print("  ❌ 期货 K 线失败:", e)
