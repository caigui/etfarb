# -*- coding: utf-8 -*-
"""诊断：兄弟反推的两个 bug。

Bug A：用「隐含净值的绝对数」取中位数 —— 不同 ETF 净值水平不同（102.09 / 101.90），
       绝对数中位数没有意义。应改成「涨跌幅中位数」再乘自己的锚点。
Bug B：昨日刚分红的兄弟（511110 于 10-22、551500 于 11-16）会把分红当成涨幅。
       要确认：ETF 分红时 pre_close（昨收）是否已除息？
"""
import io, sys, os, re, ssl, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bond_pcf_valuation as bpv
import bond_cash_etf_valuation as cbe

_CTX = ssl.create_default_context(); _CTX.check_hostname = False; _CTX.verify_mode = ssl.CERT_NONE

print("=" * 100)
print("A  各标的的 昨收 / 昨净值 / 净值日 / 是否有分红")
print("=" * 100)
CODES = list(cbe.CASH_BOND_ETF_CODES)
for c in CODES:
    q = cbe._etf_quote(c)
    navs = cbe._nav_series(c, days=10)
    if not q or not navs:
        print("  %-7s 取数失败" % c)
        continue
    nd, dwjz, ljjz, fhsp = navs[0]
    # 找分红日
    divs = [(x[0], x[3]) for x in navs if x[3]]
    print("  %-7s 现价=%-9s 昨收=%-9s | 净值日=%s DWJZ=%-9s LJJZ=%-8s 分红=%s"
          % (c, q["price"], q["pre_close"], nd, dwjz, ljjz, divs if divs else "无"))
    # 若昨日分红，看 pre_close 是否已除息
    if divs and divs[0][0] == nd:
        print("      ⚠️ 昨日(%s)分红：%s" % (nd, divs[0][1]))
        print("         昨收 %.4f vs 昨净值 %.4f  差 %.4f（若≈分红额说明价格也已除息）"
              % (q["pre_close"], dwjz, q["pre_close"] - dwjz))

print()
print("=" * 100)
print("B  正确口径：涨幅中位数（应接近真实指数涨幅 ~0.55%）")
print("=" * 100)
for c in CODES:
    meta = cbe.CASH_BOND_ETF_META[c]
    peers = meta.get("peers") or []
    if not peers:
        print("  %-7s 无兄弟" % c)
        continue
    pcts, det = [], []
    for p in peers:
        q = cbe._etf_quote(p)
        if not q:
            continue
        pct = (q["price"] / q["pre_close"] - 1) * 100
        pcts.append(pct)
        det.append("%s %+.3f%%" % (p, pct))
    if pcts:
        pcts.sort()
        med = pcts[len(pcts) // 2] if len(pcts) % 2 else \
            (pcts[len(pcts)//2-1] + pcts[len(pcts)//2]) / 2
        print("  %-7s 兄弟涨幅: %-45s → 中位 %+.4f%%" % (c, " ".join(det), med))

print()
print("=" * 100)
print("C  分红日的「价格涨幅 vs 净值涨幅」对照（验证除息是否同步）")
print("=" * 100)
for c in ("511110", "551500"):
    print("\n  -- %s --" % c)
    navs = cbe._nav_series(c, days=12)
    for i in range(1, min(4, len(navs))):
        nd, dw, lj, fhsp = navs[i - 1]
        pd, pw, plj, _ = navs[i]
        nav_chg = (dw / pw - 1) * 100 if pw else None
        cum_chg = (lj / plj - 1) * 100 if (lj and plj) else None
        print("     %s→%s  DWJZ %+.3f%%  累计 %s  分红=%s"
              % (pd, nd, nav_chg if nav_chg is not None else 0,
                 ("%+.3f%%" % cum_chg) if cum_chg is not None else "N/A",
                 fhsp or ""))
