# -*- coding: utf-8 -*-
"""只读探测 15：确认 551030 科创债ETF鹏华 的跟踪指数 + 全部 9 只的指数归属。
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


def idx_perf(code, days=60):
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    u = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
         "?indexCode=%s&startDate=%s&endDate=%s" % (code, start, end))
    j = json.loads(_req(u, referer="https://www.csindex.com.cn/"))
    out = {}
    for r in j.get("data") or []:
        td = str(r.get("tradeDate") or "")
        if len(td) == 8:
            out["%s-%s-%s" % (td[:4], td[4:6], td[6:8])] = {
                "close": r.get("close"), "pct": r.get("changePct"),
                "name": r.get("indexNameCnAll"), "cons": r.get("consNumber")}
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


print("=" * 100)
print("A  551030 候选指数扫描（上证 AAA 科创债系列）")
print("=" * 100)
# 上证AAA科创债指数可能代码
CANDS = ["950240", "950241", "950242", "950243", "950244", "950247", "950248",
         "950249", "950250", "950251", "950252",
         "932160", "932161", "932162", "932163", "932164", "932165",
         "932170", "932171", "932172", "932173", "932174", "932175"]
found = {}
for c in CANDS:
    d = idx_perf(c, days=30)
    if d:
        nm = next(iter(d.values()))["name"]
        latest = d[max(d)]
        found[c] = (nm, latest["close"], latest["pct"], latest["cons"])
        print("  %-8s %-32s close=%-10s pct=%-8s cons=%s"
              % (c, nm, latest["close"], latest["pct"], latest["cons"]))

print()
print("=" * 100)
print("B  用净值涨跌反向匹配 551030 的指数")
print("=" * 100)
nj = nav_cum("551030")
nd = sorted(nj)
print("  551030 净值日 %d 条，最近 5 日：%s" % (len(nd), [(d, nj[d]) for d in nd[-5:]]))
print("  对应日净值涨跌(bp)：")
for i in range(1, len(nd)):
    d0, d1 = nd[i - 1], nd[i]
    print("     %s  %+.2f bp" % (d1, (nj[d1] / nj[d0] - 1) * 10000))

print("\n  各候选指数同期涨跌(%)：")
for c, (nm, cl, pc, cons) in found.items():
    d = idx_perf(c, days=45)
    common = sorted(set(nj) & set(d))
    if len(common) < 5:
        continue
    errs = []
    for i in range(1, len(common)):
        d0, d1 = common[i - 1], common[i]
        if d[d1]["pct"] is None:
            continue
        actual = (nj[d1] / nj[d0] - 1) * 100
        errs.append((d[d1]["pct"] - actual) * 100)
    if errs:
        ae = sorted(abs(x) for x in errs)
        print("     %-8s %-32s n=%-2d |误差|中位=%6.2f bp  max=%6.2f bp"
              % (c, nm, len(errs), ae[len(ae) // 2], ae[-1]))

print()
print("=" * 100)
print("C  确认 8 只已定标的的指数名称")
print("=" * 100)
for c, ic in [("511070", "950245"), ("511110", "950245"), ("511190", "950245"),
              ("511200", "950245"), ("551900", "932158"), ("551500", "932158"),
              ("551550", "932158"), ("159600", "932158")]:
    d = idx_perf(ic, days=30)
    if d:
        v = d[max(d)]
        print("  %-7s → %-8s %-32s cons=%s" % (c, ic, v["name"], v["cons"]))
