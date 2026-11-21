# -*- coding: utf-8 -*-
"""只读探测 6：三个决定性问题。

A. 511110 的 -2.00% 是分红吗？全量扫 9 只的分红记录
B. PCF 成份券「券名 → 发行年 + 期限」解析可行性（代码前两位不可用）
C. 银行间信用债的评级与期限匹配（能否做 AAA 短久期代理）
"""
import io, sys, os, json, re, ssl, urllib.request
from datetime import date, timedelta
from collections import Counter
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


CODES = ["511070", "511110", "511190", "511200", "551900", "551030", "551500", "551550", "159600"]

print("=" * 100)
print("A  9 只标的的分红记录（近 60 个净值日）")
print("=" * 100)
for c in CODES:
    try:
        url = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
               "&pageIndex=1&pageSize=60" % c)
        raw = _txt(url, referer="https://fundf10.eastmoney.com/jjjz_%s.html" % c)
        s, e = raw.find("{"), raw.rfind("}") + 1
        j = json.loads(raw[s:e])
        lst = j.get("Data", {}).get("LSJZList", [])
        divs = [(r.get("FSRQ"), r.get("FHSP"), r.get("FHFCZ")) for r in lst
                if (r.get("FHSP") or "").strip() or (r.get("FHFCZ") or "").strip()]
        print("  %-7s 净值日数=%-3d 分红次数=%d %s"
              % (c, len(lst), len(divs), divs if divs else ""))
        if divs:
            # 找出分红当日的净值变动
            dd = {r["FSRQ"]: r for r in lst}
            for d, sp, fcfz in divs:
                idx = [i for i, r in enumerate(lst) if r["FSRQ"] == d]
                if idx and idx[0] + 1 < len(lst):
                    cur, prev = lst[idx[0]], lst[idx[0] + 1]
                    try:
                        navc, navp = float(cur["DWJZ"]), float(prev["DWJZ"])
                        ljjc, ljjp = float(cur["LJJZ"]), float(prev["LJJZ"])
                        print("        %s  单位净值 %.4f→%.4f (%.3f%%)  累计 %.4f→%.4f (%.3f%%)  派息=%s"
                              % (d, navp, navc, (navc / navp - 1) * 100,
                                 ljjp, ljjc, (ljjc / ljjp - 1) * 100, sp))
                    except Exception:
                        pass
    except Exception as e:
        print("  %-7s ❌ %s %s" % (c, type(e).__name__, str(e)[:80]))

print()
print("=" * 100)
print("B  PCF 券名 → 发行年 解析（验证代码前两位是否可用）")
print("=" * 100)
import bond_sse_pcf_valuation as bsv

pcf = bsv.fetch_sse_pcf("511070")
bonds = [b for b in pcf["bonds"] if b.get("qty", 0) > 0]
print("  511070 共 %d 只，前 20 只的 代码 vs 券名:" % len(bonds))
for b in bonds[:20]:
    bid, nm = str(b.get("id")), str(b.get("name") or "")
    print("    %-10s %-24s  代码前2位=%s  券名前2位=%s"
          % (bid, nm, bid[:2], nm[:2]))

print("\n  代码前两位 vs 券名前两位 交叉分布（全 313 只）：")
cross = Counter((str(b["id"])[:2], str(b.get("name") or "")[:2]) for b in bonds)
for (a, bb), n in sorted(cross.items(), key=lambda x: -x[1])[:20]:
    print("    代码前2位=%-3s  券名前2位=%-4s  n=%d" % (a, bb, n))

print("\n  券名前两位分布（应为发行年尾数）：")
yrs = Counter(str(b.get("name") or "")[:2] for b in bonds)
print("    ", dict(sorted(yrs.items())))

print()
print("=" * 100)
print("C  银行间信用债：评级 / 期限 / 品种（能否做 AAA 短久期代理）")
print("=" * 100)
import bond_pcf_valuation as bpv
spot = bpv.fetch_spot_deal()
print("  行情日 %s  总条数 %d\n" % (bpv._latest_spot_date(spot), len(spot)))

# 全部有 Δy 的信用类样本，按期限桶统计
import math
groups = {
    "中票MTN": [k for k in spot if "MTN" in k],
    "二级资本债": [k for k in spot if "二级资本债" in k],
    "CD": [k for k in spot if "CD" in k and "MTN" not in k],
    "科创债": [k for k in spot if "科创债" in k],
    "PPN": [k for k in spot if "PPN" in k],
}
for g, ks in groups.items():
    rows = [(k, spot[k]) for k in ks if spot[k].get("dy_bp") is not None]
    if not rows:
        print("  %-12s 无样本" % g)
        continue
    bl = {}
    for k, v in rows:
        t = v.get("term") or 0
        key = "<=1y" if t <= 1 else "1-2y" if t <= 2 else "2-3y" if t <= 3 \
            else "3-5y" if t <= 5 else ">5y"
        bl.setdefault(key, []).append(v["dy_bp"])
    s = []
    for key in ("<=1y", "1-2y", "2-3y", "3-5y", ">5y"):
        xs = bl.get(key)
        if xs:
            xs.sort()
            s.append("%s n=%d 中位%+.2f" % (key, len(xs), xs[len(xs) // 2]))
    print("  %-12s n=%-3d  %s" % (g, len(rows), " | ".join(s)))

# 2-3y 桶的中票明细
print("\n  中票 2-3y 桶明细（这批 ETF 的目标期限段）：")
sel = [(k, v) for k, v in spot.items() if "MTN" in k and v.get("dy_bp") is not None
       and 2.0 <= (v.get("term") or 0) <= 3.0]
for k, v in sel[:15]:
    print("    %-28s term=%-5s yld=%-7s Δy=%+7.2f" % (k, v.get("term"), v.get("yield"), v["dy_bp"]))
print("    n=", len(sel))

print()
print("=" * 100)
print("D  PCF 里 est_cash / navpercu 的符号（影响债券占比计算）")
print("=" * 100)
for c in CODES[:8]:
    try:
        p = bsv.fetch_sse_pcf(c)
        npcu, cash = p.get("navpercu"), p.get("est_cash")
        frac = (npcu - (cash or 0)) / npcu if npcu else None
        print("  %-7s navpercu=%-14s est_cash=%-12s 债券占比=%.4f  NAV=%.4f unit=%s"
              % (c, npcu, cash, frac if frac else -1, p.get("nav") or 0, p.get("unit")))
    except Exception as e:
        print("  %-7s ❌ %s" % (c, str(e)[:60]))
