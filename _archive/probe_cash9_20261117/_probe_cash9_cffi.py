# -*- coding: utf-8 -*-
"""只读探测 17：用项目 curl_cffi 指纹通道取中证 perf（绕过 WAF 403）。

并定死 9 只标的的跟踪指数（在 932158 / 932160 / 932163 / 950245 中择优）。
结果同时缓存到 data/_probe/csi_perf_*.json，供后续实现复用与对比。
"""
import io, sys, os, json, time, statistics
from datetime import date, timedelta
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bond_pcf_valuation as bpv

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "_probe")
CACHE = os.path.abspath(CACHE)
os.makedirs(CACHE, exist_ok=True)


def csi_perf(code, days=45, ttl=1800):
    """中证 perf 经 curl_cffi（指纹伪装）取，落盘缓存避免重复打 WAF。"""
    fp = os.path.join(CACHE, "csi_%s_%dd.json" % (code, days))
    if os.path.exists(fp) and time.time() - os.path.getmtime(fp) < ttl:
        try:
            with open(fp, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    u = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
         "?indexCode=%s&startDate=%s&endDate=%s" % (code, start, end))
    out, err = {}, None
    for attempt in range(3):
        try:
            r = bpv._req("get", u, timeout=20,
                         headers={"User-Agent": bpv._UA,
                                  "Referer": "https://www.csindex.com.cn/"})
            j = json.loads(r.text)
            for row in j.get("data") or []:
                td = str(row.get("tradeDate") or "")
                if len(td) == 8:
                    out["%s-%s-%s" % (td[:4], td[4:6], td[6:8])] = {
                        "close": row.get("close"), "pct": row.get("changePct"),
                        "name": row.get("indexNameCnAll"), "cons": row.get("consNumber")}
            if out:
                break
        except Exception as e:
            err = "%s %s" % (type(e).__name__, str(e)[:80])
        time.sleep(1.5 * (attempt + 1))
    if out:
        try:
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
        except Exception:
            pass
    elif err:
        print("     [csi_perf %s 失败] %s" % (code, err))
    return out


def nav_cum(code, size=120):
    u = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
         "&pageIndex=1&pageSize=%d" % (code, size))
    r = bpv._req("get", u, timeout=15, headers={
        "User-Agent": bpv._UA,
        "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code})
    t = r.text
    s, e = t.find("{"), t.rfind("}") + 1
    j = json.loads(t[s:e])
    return {x["FSRQ"]: float(x["LJJZ"]) for x in j.get("Data", {}).get("LSJZList", [])
            if x.get("LJJZ")}


print("=" * 100)
print("STEP 1  取 4 个候选指数（curl_cffi 通道）")
print("=" * 100)
IDXS = {}
for c in ("950245", "932158", "932160", "932163"):
    d = csi_perf(c)
    IDXS[c] = d
    s = sorted(d)
    if d:
        v = d[s[-1]]
        print("  %-8s %-34s %2d 条 [%s ~ %s] close=%s pct=%s cons=%s"
              % (c, v["name"], len(s), s[0], s[-1], v["close"], v["pct"], v["cons"]))
    else:
        print("  %-8s ❌ 无数据" % c)

if not any(IDXS.values()):
    print("\n⚠️ 全部指数取数失败，无法继续")
    sys.exit(1)

print()
print("=" * 100)
print("STEP 2  逐只择优（|误差|中位 → RMSE → max 三级排序）")
print("=" * 100)
CODES = ["511070", "511110", "511190", "511200",
         "551900", "551500", "551550", "159600", "551030"]
best = {}
for code in CODES:
    nj = nav_cum(code)
    print("\n  【%s】净值 %d 条 [%s ~ %s]" % (code, len(nj), min(nj), max(nj)))
    rows = []
    for ic, ix in IDXS.items():
        if not ix:
            continue
        common = sorted(set(nj) & set(ix))
        errs = []
        for i in range(1, len(common)):
            d0, d1 = common[i - 1], common[i]
            if ix[d1]["pct"] is None:
                continue
            actual = (nj[d1] / nj[d0] - 1) * 100
            errs.append((ix[d1]["pct"] - actual) * 100)
        if len(errs) < 8:
            continue
        ae = sorted(abs(x) for x in errs)
        rmse = (sum(x * x for x in errs) / len(errs)) ** 0.5
        rows.append({"ic": ic, "n": len(errs), "med": ae[len(ae) // 2],
                     "rmse": rmse, "max": ae[-1], "bias": statistics.median(errs),
                     "name": ix[max(ix)]["name"]})
    rows.sort(key=lambda r: (r["med"], r["rmse"]))
    for i, r in enumerate(rows):
        print("     %s %-8s n=%-3d |误差|中位=%6.3f  RMSE=%6.3f  max=%7.3f  偏差=%+6.3f  %s"
              % ("★" if i == 0 else " ", r["ic"], r["n"], r["med"], r["rmse"],
                 r["max"], r["bias"], r["name"]))
    if rows:
        best[code] = (rows[0]["ic"], rows[0]["name"], rows[0]["med"], rows[0]["rmse"])
    else:
        print("     ❌ 无可用指数")

print()
print("=" * 100)
print("定案")
print("=" * 100)
for c in CODES:
    b = best.get(c)
    if b:
        print("  %-7s → %-8s %-34s |误差|中位=%.3f bp RMSE=%.3f bp"
              % (c, b[0], b[1], b[2], b[3]))
    else:
        print("  %-7s → ❌ 未定" % c)

out = os.path.join(CACHE, "cash9_index_map.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump({c: {"index": b[0], "name": b[1], "med_bp": b[2], "rmse_bp": b[3]}
               for c, b in best.items()}, f, ensure_ascii=False, indent=1)
print("\n定案已落盘：", out)
