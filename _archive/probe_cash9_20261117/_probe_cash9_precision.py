# -*- coding: utf-8 -*-
"""只读探测 14（定案）：中证指数日涨跌 vs ETF 净值涨跌 精度回测。

已确认正确的跟踪指数：
  511070 / 511110 / 511190 / 511200 → 950245 上证基准做市公司债指数
  551900 / 551500 / 551550 / 159600 → 932158 中证科技创新公司债指数
  551030                          → 上证AAA科创债（待确认，可能是 932159 中证高等级）

回测每日：index.changePct vs ETF 累计净值涨跌，算误差分布。
若 |误差| 中位 < 5bp → 可用作估值驱动。
"""
import io, sys, os, json, ssl, urllib.request, gzip, statistics
from datetime import date, timedelta
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bond_pcf_valuation as bpv

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _req(url, referer=None, timeout=20):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    b = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                               timeout=timeout, context=_CTX).read()
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b.decode("utf-8", "ignore")


def idx_perf(index_code, days=60):
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    u = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
         "?indexCode=%s&startDate=%s&endDate=%s" % (index_code, start, end))
    j = json.loads(_req(u, referer="https://www.csindex.com.cn/"))
    out = {}
    for r in j.get("data") or []:
        td = str(r.get("tradeDate") or "")
        if len(td) == 8:
            d = "%s-%s-%s" % (td[:4], td[4:6], td[6:8])
            out[d] = {"close": r.get("close"), "pct": r.get("changePct"),
                      "chg": r.get("change"), "cons": r.get("consNumber")}
    return out


def nav_cum(code, size=60):
    u = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
         "&pageIndex=1&pageSize=%d" % (code, size))
    req = urllib.request.Request(u, headers={
        "User-Agent": bpv._UA, "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code})
    raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
    s, e = raw.find("{"), raw.rfind("}") + 1
    j = json.loads(raw[s:e])
    return {r["FSRQ"]: float(r["LJJZ"]) for r in j.get("Data", {}).get("LSJZList", []) if r.get("LJJZ")}


MAP = {
    "511070": ["950245"],
    "511110": ["950245"],
    "511190": ["950245"],
    "511200": ["950245"],
    "551900": ["932158", "932159"],
    "551500": ["932158", "932159"],
    "551550": ["932158", "932159"],
    "159600": ["932158", "932159"],
    "551030": ["951", "932159", "932158"],   # 上证AAA科创债待定
}

print("=" * 100)
print("STEP 1  指数日线（近 45 天）")
print("=" * 100)
IDX = {}
for c in ("950245", "932158", "932159", "932081", "950246"):
    IDX[c] = idx_perf(c)
    s = sorted(IDX[c])
    print("  %-8s %2d 条 [%s ~ %s]  最新 close=%s pct=%s"
          % (c, len(s), s[0] if s else "-", s[-1] if s else "-",
             IDX[c][s[-1]]["close"] if s else "-", IDX[c][s[-1]]["pct"] if s else "-"))

print()
print("=" * 100)
print("STEP 2  指数涨跌 vs ETF 净值涨跌（回测）")
print("=" * 100)
for code, cands in MAP.items():
    nj = nav_cum(code)
    if not nj:
        print("  %-7s 净值空" % code)
        continue
    print("\n  【%s】净值日 %d 条 [%s ~ %s]" % (code, len(nj), min(nj), max(nj)))
    for ic in cands:
        if len(ic) < 6 or ic not in IDX:
            continue
        ix = IDX[ic]
        dates = sorted(set(nj) & set(ix))
        dates = [d for d in dates if ix[d]["pct"] is not None]
        errs, rows = [], []
        for i in range(1, len(dates)):
            d0, d1 = dates[i - 1], dates[i]
            n0, n1 = nj[d0], nj[d1]
            if not n0 or not n1:
                continue
            actual = (n1 / n0 - 1) * 100
            proxy = ix[d1]["pct"]
            e = (proxy - actual) * 100        # bp
            errs.append(e)
            rows.append((d1, proxy, actual, e))
        if not errs:
            print("     %-8s 无有效样本" % ic)
            continue
        ae = sorted(abs(x) for x in errs)
        print("     %-8s n=%-2d 误差中位=%+6.2f bp  |误差|中位=%5.2f bp  |误差|P90=%6.2f bp  max=%6.2f bp"
              % (ic, len(errs), statistics.median(errs), ae[len(ae) // 2],
                 ae[int(len(ae) * 0.9)], ae[-1]))
        for d, p, a, e in rows[-4:]:
            print("          %s 指数=%+.4f%% 净值=%+.4f%% 误差=%+.1f bp" % (d, p, a, e))
        if ic == cands[0]:
            print("          ↑ 首选指数")
