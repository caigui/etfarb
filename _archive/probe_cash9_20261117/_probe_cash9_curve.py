# -*- coding: utf-8 -*-
"""只读探测 11（最终决策）：CFETS 政金债 Δy 能否解释这批 ETF 的净值涨跌？

逻辑：短久期 AAA 信用债的日波动 ≈ 同期限利率债日波动 + 信用利差变动（后者小）。
      CFETS 估值表提供**政金债/国债的真实日间收益率**（可靠，不像货币网 bpNum 基准漂移）。
      若「政金债 Δy × 组合久期」能解释 ETF 净值涨跌 → 这就是可用驱动。

同时用回归反推「有效久期」，与券名年份推算的久期对照。
"""
import io, sys, os, json, re, ssl, urllib.request, statistics
from datetime import date, timedelta
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bond_pcf_valuation as bpv
import bond_sse_pcf_valuation as bsv

CODES = ["511070", "511110", "511190", "511200", "551900", "551030", "551500", "551550", "159600"]

print("=" * 100)
print("STEP 1  取 CFETS 估值（多日）")
print("=" * 100)
today = date.today()
vals = {}
for k in range(0, 10):
    d = (today - timedelta(days=k)).isoformat()
    try:
        v = bpv.fetch_cfets_valuation(d)
        if v:
            vals[d] = v
            print("  %s -> %d 条" % (d, len(v)))
    except Exception:
        pass
days = sorted(vals)
print("  可用日：", days)

if len(days) < 3:
    print("⚠️ CFETS 日期不足")
    sys.exit(0)


def curve_dy(d0, d1, lo=1.5, hi=3.0, kinds=("国开", "农发", "进出口", "国债")):
    """两日之间，指定期限段内的政金债 Δy 中位数（bp）。"""
    a, b = vals[d0], vals[d1]
    out = []
    for code in set(a) & set(b):
        ra, rb = a[code], b[code]
        nm = str(ra.get("name") or "")
        if not any(k in nm for k in kinds):
            continue
        ya, yb = ra.get("y"), rb.get("y")
        dur = rb.get("dur")
        if ya is None or yb is None or not dur:
            continue
        if not (lo <= dur <= hi):
            continue
        out.append(((yb - ya) * 100.0, dur, nm, code))
    if not out:
        return None
    dys = sorted(x[0] for x in out)
    return {"dy": dys[len(dys) // 2], "n": len(out),
            "mean": sum(dys) / len(dys), "lo": dys[0], "hi": dys[-1],
            "wdur": sum(x[1] for x in out) / len(out)}


print()
print("=" * 100)
print("STEP 2  各期限段政金债的日间 Δy（bp，中位数）")
print("=" * 100)
for i in range(1, len(days)):
    d0, d1 = days[i - 1], days[i]
    print("\n  %s → %s" % (d0, d1))
    for lo, hi, lbl in ((0, 1, "0-1y"), (1, 2, "1-2y"), (2, 3, "2-3y"),
                        (3, 5, "3-5y"), (5, 10, "5-10y")):
        r = curve_dy(d0, d1, lo, hi)
        if r:
            print("     %-6s n=%-3d Δy中位=%+6.2f  均值=%+6.2f  [%+.2f~%+.2f]  平均久期=%.2f"
                  % (lbl, r["n"], r["dy"], r["mean"], r["lo"], r["hi"], r["wdur"]))


def nav_cum(code, size=40):
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    u = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s&pageIndex=1&pageSize=%d"
         % (code, size))
    req = urllib.request.Request(u, headers={
        "User-Agent": bpv._UA, "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code})
    raw = urllib.request.urlopen(req, timeout=10, context=ctx).read().decode("utf-8", "ignore")
    s, e = raw.find("{"), raw.rfind("}") + 1
    j = json.loads(raw[s:e])
    return {r["FSRQ"]: float(r["LJJZ"]) for r in j.get("Data", {}).get("LSJZList", []) if r.get("LJJZ")}


print()
print("=" * 100)
print("STEP 3  回归反推有效久期：ETF 净值涨跌 vs 2-3y 政金债 Δy")
print("=" * 100)
NAV = {c: nav_cum(c) for c in CODES}
print("  净值日范围：%s ~ %s" % (min(min(v) for v in NAV.values() if v),
                                max(max(v) for v in NAV.values() if v)))

for c in CODES:
    nj = NAV.get(c) or {}
    pairs = []
    for i in range(1, len(days)):
        d0, d1 = days[i - 1], days[i]
        n0, n1 = nj.get(d0), nj.get(d1)
        if not n0 or not n1:
            continue
        r = curve_dy(d0, d1, 1.0, 3.5)
        if not r:
            continue
        actual_bp = (n1 / n0 - 1) * 10000.0
        pairs.append((d1, r["dy"], actual_bp))
    if len(pairs) < 5:
        print("  %-7s 样本不足 %d" % (c, len(pairs)))
        continue
    xs = [p[1] for p in pairs]
    ys = [p[2] for p in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    beta = sxy / sxx if sxx else None
    # 模型：ΔP(bp) = -dur × Δy(bp)  →  dur = -beta
    resids = [y - (beta * x + (my - beta * mx)) for x, y in zip(xs, ys)]
    rmse = (sum(r * r for r in resids) / len(resids)) ** 0.5
    print("  %-7s n=%2d  隐含久期=%.2f  残差RMSE=%.2f bp  实际涨跌[%+.1f~%+.1f] bp  Δy[%+.1f~%+.1f] bp"
          % (c, len(pairs), -beta if beta else 0, rmse,
             min(ys), max(ys), min(xs), max(xs)))
    print("        明细: " + ", ".join("%s Δy%+.1f→%+.1fbp" % (d[5:], x, y) for d, x, y in pairs[-5:]))
