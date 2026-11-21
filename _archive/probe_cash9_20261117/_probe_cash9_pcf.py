# -*- coding: utf-8 -*-
"""只读探测：9 只新增现金 ETF 的 PCF / 净值 / 指数 / 成份券可得性。

判定每个代码能否复用 bond_sse_pcf_valuation 的 chain，还是需要另建路径。
不写任何文件、不改数据库。
"""
import io, sys, os, json, traceback
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CODES = [
    ("511070", "公司债ETF南方",   "sse", "沪做市公司债 950245"),
    ("511110", "公司债ETF易方达", "sse", "沪做市公司债 950245"),
    ("511190", "信用债ETF海富通", "sse", "沪做市公司债 950245"),
    ("511200", "信用债ETF华夏",   "sse", "沪做市公司债 950245"),
    ("551900", "科创债ETF招商",   "sse", "中证AAA科创债"),
    ("551030", "科创债ETF鹏华",   "sse", "上证AAA科创债"),
    ("551500", "科创债ETF易方达", "sse", "中证AAA科创债"),
    ("551550", "科创债ETF华夏",   "sse", "中证AAA科创债"),
    ("159600", "科创债ETF嘉实",   "szse", "中证AAA科创债"),
]

print("=" * 100)
print("STEP 1  上交所官方 PCF 下载口（downloadETF2Bulletin.do?fundCode=xxx）")
print("=" * 100)

import bond_pcf_valuation as bpv
import bond_sse_pcf_valuation as bsv

for code, name, mkt, idx in CODES:
    if mkt != "sse":
        print("%-8s %-16s [跳过] 深市，上交所口不适用" % (code, name))
        continue
    try:
        txt = bpv._get(bsv.SSE_BULLETIN_URL % code, referer=bsv.SSE_REFERER,
                       timeout=15, impersonate=None)
        n = txt.count("<Component>")
        pcf = bsv._parse_sse_xml(txt)
        print("%-8s %-16s ✅ 长度=%d 成份=%d TradingDay=%s NAV=%s unit=%s navpercu=%s est_cash=%s"
              % (code, name, len(txt), n, pcf.get("trading_day"), pcf.get("nav"),
                 pcf.get("unit"), pcf.get("navpercu"), pcf.get("est_cash")))
        # 打前 5 只成份券
        for b in pcf["bonds"][:5]:
            print("        %-10s %-24s qty=%s flag=%s"
                  % (b.get("id"), b.get("name"), b.get("qty"), b.get("flag")))
        if len(pcf["bonds"]) > 5:
            print("        ... 共 %d 只" % len(pcf["bonds"]))
    except Exception as e:
        print("%-8s %-16s ❌ %s: %s" % (code, name, type(e).__name__, str(e)[:120]))

print()
print("=" * 100)
print("STEP 2  东财净值历史（锚点）")
print("=" * 100)
for code, name, mkt, idx in CODES:
    try:
        h = bsv._nav_history(code, days=8)
        print("%-8s %-16s %s" % (code, name,
              " ✅ " + ", ".join("%s=%.4f" % (d, v) for d, v in h[:4]) if h else " ❌ 空"))
    except Exception as e:
        print("%-8s %-16s ❌ %s: %s" % (code, name, type(e).__name__, str(e)[:100]))

print()
print("=" * 100)
print("STEP 3  货币网现券成交里的信用债关键字覆盖（判断代理可用性）")
print("=" * 100)
try:
    spot = bpv.fetch_spot_deal()
    print("现券总条数：", len(spot), " 行情日：", bpv._latest_spot_date(spot))
    for kw in ("城投", "公司债", "科创", "中票", "MTN", "PPN", "短融", "超短融",
               "CD", "二级资本债", "金融债", "AAA"):
        hit = [k for k in spot if kw in k]
        withdy = [k for k in hit if spot[k].get("dy_bp") is not None]
        print("  %-8s 命中 %4d  有Δy %4d  例: %s"
              % (kw, len(hit), len(withdy), (hit[:3] if hit else "")))
except Exception as e:
    print("❌ fetch_spot_deal 失败:", e)
    traceback.print_exc()
