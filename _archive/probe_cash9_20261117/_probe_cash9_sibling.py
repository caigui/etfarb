# -*- coding: utf-8 -*-
"""只读探测 8（决定性）：兄弟 ETF 平均涨跌能否作为指数代理？

假设：跟踪同一指数的 N 只 ETF，其「平均收盘涨跌」≈ 指数涨跌（个体折溢价互相抵消）。
若成立 → 可用兄弟 ETF 做驱动，算出与自己价格独立的估值，折溢价才有意义。

回测设计：
  对每个交易日 d（近 25 日）：
    proxy_chg(d) = 兄弟 ETF（同指数、排除自己）在 d 日的收盘涨跌均值
    actual_chg(d) = 自己 d 日净值涨跌（累计净值口径，剔除分红影响）
    err(d) = proxy_chg(d) - actual_chg(d)
  统计 err 的中位/均值/标准差，判断可用性。
"""
import io, sys, os, json, re, ssl, urllib.request, gzip, statistics
from datetime import date, timedelta
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _raw(url, referer=None, timeout=15):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                timeout=timeout, context=_CTX)
    b = r.read()
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b


def _txt(url, referer=None, timeout=15):
    return _raw(url, referer, timeout).decode("utf-8", "ignore")


# 分组：跟踪同一指数的 ETF
GROUPS = {
    "沪做市公司债 950245": ["511070", "511110", "511190", "511200"],
    "中证AAA科创债": ["551900", "551500", "551550", "159600"],
    "上证AAA科创债": ["551030"],
}
ALL = [c for g in GROUPS.values() for c in g]


def kline(code, lmt=40):
    """东财日K收盘价 {date: close}。"""
    secid = ("1." if code[0] in "56" else "0.") + code
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
         "secid=%s&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
         "&klt=101&fqt=0&end=20500101&lmt=%d" % (secid, lmt))
    out = {}
    try:
        j = json.loads(_txt(u, referer="https://quote.eastmoney.com/"))
        for ln in (j.get("data") or {}).get("klines") or []:
            f = ln.split(",")
            out[f[0]] = float(f[2])          # f51=date f52=open f53=close
    except Exception as e:
        print("    kline %s ❌ %s" % (code, str(e)[:60]))
    return out


def nav_cum(code, size=80):
    """东财累计净值 {date: LJJZ}（剔除分红影响，用于算真实涨跌）。"""
    u = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
         "&pageIndex=1&pageSize=%d" % (code, size))
    out = {}
    try:
        raw = _txt(u, referer="https://fundf10.eastmoney.com/jjjz_%s.html" % code)
        s, e = raw.find("{"), raw.rfind("}") + 1
        j = json.loads(raw[s:e])
        for r in j.get("Data", {}).get("LSJZList", []):
            try:
                out[r["FSRQ"]] = float(r["LJJZ"])
            except (TypeError, ValueError, KeyError):
                pass
    except Exception as e:
        print("    nav %s ❌ %s" % (code, str(e)[:60]))
    return out


print("=" * 100)
print("取数：9 只标的的日K收盘价 + 累计净值")
print("=" * 100)
KL, NAV = {}, {}
for c in ALL:
    KL[c] = kline(c)
    NAV[c] = nav_cum(c)
    print("  %-7s K线 %2d 根 [%s ~ %s]   净值 %2d 条 [%s ~ %s]"
          % (c, len(KL[c]), min(KL[c]) if KL[c] else "-", max(KL[c]) if KL[c] else "-",
             len(NAV[c]), min(NAV[c]) if NAV[c] else "-", max(NAV[c]) if NAV[c] else "-"))

# 自检：K 线日期与净值日期是否对齐
print("\n  日期自检（511070 最近 6 个 K 线 vs 净值）：")
ds = sorted(KL["511070"])[-6:]
for d in ds:
    print("     %s  close=%-9s  nav_cum=%s" % (d, KL["511070"].get(d), NAV["511070"].get(d)))

print()
print("=" * 100)
print("回测：兄弟 ETF 平均涨跌（代理） vs 自己净值涨跌（实际）")
print("=" * 100)

summary = []
for gname, members in GROUPS.items():
    if len(members) < 2:
        print("\n  【%s】仅 1 只，无兄弟可比 → 无法回测，需另找驱动" % gname)
        continue
    print("\n  【%s】成员 %s" % (gname, ",".join(members)))
    for code in members:
        sibs = [x for x in members if x != code]
        # 全市场共同交易日（K线有收盘价的日期）
        dates = sorted(set(KL[code]) & set.intersection(*[set(KL[s]) for s in sibs]))
        dates = [d for d in dates if d in NAV[code]]
        errs, rows = [], []
        for i in range(1, len(dates)):
            d0, d1 = dates[i - 1], dates[i]
            # 代理：兄弟 ETF 的收盘涨跌均值
            pcs = []
            for s in sibs:
                a, b = KL[s].get(d0), KL[s].get(d1)
                if a and b:
                    pcs.append((b / a - 1) * 100)
            if not pcs:
                continue
            proxy = sum(pcs) / len(pcs)
            # 实际：自己累计净值涨跌（LJJZ 已含分红再投，等价于真实收益）
            n0, n1 = NAV[code].get(d0), NAV[code].get(d1)
            if not n0 or not n1:
                continue
            actual = (n1 / n0 - 1) * 100
            e = proxy - actual
            errs.append(e)
            rows.append((d1, proxy, actual, e))
        if not errs:
            print("     %-7s 无有效样本" % code)
            continue
        ae = sorted(abs(x) for x in errs)
        print("     %-7s n=%-3d 误差(bp) 中位=%+7.2f  均值=%+7.2f  |误差|中位=%6.2f  |误差|P90=%6.2f  |误差|max=%6.2f"
              % (code, len(errs), statistics.median(errs), sum(errs) / len(errs),
                 ae[len(ae) // 2] * 100 / 100 * 100, ae[int(len(ae) * 0.9)] * 100,
                 ae[-1] * 100))
        print("        最近 6 日明细：")
        for d, p, a, e in rows[-6:]:
            print("           %s 代理=%+.4f%% 实际=%+.4f%% 误差=%+.2f bp" % (d, p, a, e * 100))
        summary.append((code, statistics.median(errs) * 100, ae[len(ae) // 2] * 100))

print()
print("=" * 100)
print("汇总：方案可行性判定")
print("=" * 100)
if summary:
    print("  %-8s %-14s %-14s" % ("代码", "误差中位(bp)", "|误差|中位(bp)"))
    for c, m, am in summary:
        flag = "✅ 可用" if am < 5 else ("⚠️ 勉强" if am < 10 else "❌ 不可用")
        print("  %-8s %+14.2f %14.2f   %s" % (c, m, am, flag))
    print("\n  判定阈值：|误差|中位 < 5bp 可用；5~10bp 勉强（需在 detail 里标注不确定性）；>10bp 不可用")
