# -*- coding: utf-8 -*-
"""只读探测 10（决定性）：CFETS 逐券估值是否覆盖这 9 只 ETF 的成份券？

CFETS ClsBndValExcel 是全市场债券估值表，带 y(收益率)/dur(修正久期)/net(净价)。
若覆盖成份券 → 可用「昨日 vs 前日 的收益率差」逐券算 ΔP，这是真正的逐券估值。

步骤：
 1. 取 CFETS 估值（今天 + 昨天）
 2. 对 9 只 ETF 的 PCF 成份券求命中率
 3. 用命中券算「加权 Δy × 久期」→ ΔP%，与净值实际涨跌对比
"""
import io, sys, os, json, re
from datetime import date, timedelta
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bond_pcf_valuation as bpv
import bond_sse_pcf_valuation as bsv

CODES = ["511070", "511110", "511190", "511200", "551900", "551030", "551500", "551550"]

print("=" * 100)
print("STEP 1  CFETS 估值可用日期")
print("=" * 100)
today = date.today()
avail = {}
for k in range(0, 8):
    d = (today - timedelta(days=k)).isoformat()
    try:
        v = bpv.fetch_cfets_valuation(d)
        avail[d] = v
        print("  %s -> %d 条" % (d, len(v) if v else 0))
    except Exception as e:
        print("  %s ❌ %s %s" % (d, type(e).__name__, str(e)[:70]))

good = [(d, v) for d, v in sorted(avail.items(), reverse=True) if v]
if len(good) < 2:
    print("\n⚠️ CFETS 估值不足 2 个交易日，无法算 Δy")
    sys.exit(0)
d_cur, v_cur = good[0]
d_prev, v_prev = good[1]
print("\n  用 %s（%d 条） vs %s（%d 条）算 Δy" % (d_cur, len(v_cur), d_prev, len(v_prev)))

print()
print("=" * 100)
print("STEP 2  PCF 成份券的 CFETS 命中率")
print("=" * 100)
hits_all = {}
for c in CODES:
    try:
        pcf = bsv.fetch_sse_pcf(c)
    except Exception as e:
        print("  %-7s ❌ PCF %s" % (c, str(e)[:60]))
        continue
    bonds = [b for b in pcf["bonds"] if b.get("qty", 0) > 0]
    hit, miss = [], []
    for b in bonds:
        bid = str(b.get("id") or "").strip()
        if bid in v_cur and bid in v_prev:
            hit.append(b)
        else:
            miss.append(b)
    rate = len(hit) / len(bonds) * 100 if bonds else 0
    qty_hit = sum(b["qty"] for b in hit)
    qty_all = sum(b["qty"] for b in bonds) or 1
    print("  %-7s 成份=%-3d CFETS命中=%-3d (%.1f%%)  数量加权命中=%.1f%%"
          % (c, len(bonds), len(hit), rate, qty_hit / qty_all * 100))
    if miss[:3]:
        print("        未命中示例: %s" % ", ".join("%s/%s" % (b.get("id"), b.get("name")) for b in miss[:3]))
    hits_all[c] = (pcf, hit, miss)

print()
print("=" * 100)
print("STEP 3  逐券算 ΔP% 并与净值实际涨跌对比")
print("=" * 100)
print("  公式：ΔP% = -Σ(w_i × dur_i × Δy_i) / Σw_i × 100，w_i = qty×100（面值）")

# 各 ETF 在 d_cur 的实际净值涨跌（累计净值）
def nav_cum(code, size=30):
    import urllib.request, ssl
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    u = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s&pageIndex=1&pageSize=%d"
         % (code, size))
    req = urllib.request.Request(u, headers={
        "User-Agent": bpv._UA, "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code})
    raw = urllib.request.urlopen(req, timeout=10, context=ctx).read().decode("utf-8", "ignore")
    s, e = raw.find("{"), raw.rfind("}") + 1
    j = json.loads(raw[s:e])
    return {r["FSRQ"]: float(r["LJJZ"]) for r in j.get("Data", {}).get("LSJZList", [])
            if r.get("LJJZ")}

for c in CODES:
    if c not in hits_all:
        continue
    pcf, hit, miss = hits_all[c]
    if not hit:
        print("  %-7s CFETS 零命中，跳过" % c)
        continue
    num = den = 0.0
    det = []
    for b in hit:
        bid = str(b["id"]).strip()
        cur, prev = v_cur[bid], v_prev[bid]
        y0, y1 = prev.get("y"), cur.get("y")
        dur = cur.get("dur")
        if y0 is None or y1 is None or not dur:
            continue
        dy_bp = (y1 - y0) * 100.0        # 收益率% → bp
        w = b["qty"] * 100.0             # 面值
        num += w * dur * dy_bp
        den += w
        det.append((bid, b.get("name"), dy_bp, dur, w))
    if not den:
        print("  %-7s 无有效 Δy" % c)
        continue
    dpct = -(num / den) / 10000.0 * 100      # → %
    wdur = sum(d[3] * d[4] for d in det) / den
    wdy = sum(d[2] * d[4] for d in det) / den
    nj = nav_cum(c)
    a0, a1 = nj.get(d_prev), nj.get(d_cur)
    actual = (a1 / a0 - 1) * 100 if (a0 and a1) else None
    print("  %-7s 命中=%-3d 组合久期=%.2f  加权Δy=%+.2f bp  →  ΔP=%+.4f%%  实际净值=%s  误差=%s"
          % (c, len(det), wdur, wdy, dpct,
             ("%+.4f%%" % actual) if actual is not None else "N/A",
             ("%+.2f bp" % ((dpct - actual) * 100)) if actual is not None else "N/A"))
    det.sort(key=lambda x: -x[4])
    for bid, nm, dy, dur, w in det[:3]:
        print("        %-10s %-16s Δy=%+7.2f bp dur=%.2f" % (bid, nm, dy, dur))

print()
print("=" * 100)
print("STEP 4  CFETS 估值表里交易所信用债的样本量（判断覆盖广度）")
print("=" * 100)
exch = {k: v for k, v in v_cur.items() if str(k)[:1] in "12" and len(str(k)) == 6}
print("  以 1/2 开头的（交易所公司债/企业债）共 %d 只，占全表 %.1f%%"
      % (len(exch), len(exch) / len(v_cur) * 100))
ks = [k for k in exch][:8]
for k in ks:
    print("    %-10s %s" % (k, json.dumps(exch[k], ensure_ascii=False)))
