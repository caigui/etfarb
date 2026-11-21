# -*- coding: utf-8 -*-
"""只读探测 16：精确区分 932158 / 932160 / 932163，为 9 只定死跟踪指数。

932158 中证科技创新公司债指数         cons=1747
932160 中证AAA科技创新公司债指数      cons=1512
932163 中证AAA国企科技创新公司债指数   cons=1485

三只走势高度相似，必须用更大样本 + 更细指标区分。
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


def _req(url, referer=None, timeout=25):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    b = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                               timeout=timeout, context=_CTX).read()
    if b[:2] == b"\x1f\x8b":
        b = gzip.decompress(b)
    return b.decode("utf-8", "ignore")


def idx_perf(code, days=45):
    """中证 perf 接口：**单次窗口上限约 45 天**（实测 200/100 天都返回空 data）。

    短时间密集请求会被限流（返回空 data 或异常），故带 2 次重试 + 退避。
    """
    import time as _t
    out = {}
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    u = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
         "?indexCode=%s&startDate=%s&endDate=%s" % (code, start, end))
    last = None
    for attempt in range(3):
        try:
            j = json.loads(_req(u, referer="https://www.csindex.com.cn/"))
            for r in j.get("data") or []:
                td = str(r.get("tradeDate") or "")
                if len(td) == 8:
                    out["%s-%s-%s" % (td[:4], td[4:6], td[6:8])] = {
                        "close": r.get("close"), "pct": r.get("changePct"),
                        "name": r.get("indexNameCnAll")}
            if out:
                return out
            last = "data 为空"
        except Exception as e:
            last = "%s %s" % (type(e).__name__, str(e)[:60])
        _t.sleep(1.2 * (attempt + 1))
    print("     [idx_perf %s 失败] %s" % (code, last))
    return out


def nav_cum(code, size=120):
    u = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
         "&pageIndex=1&pageSize=%d" % (code, size))
    req = urllib.request.Request(u, headers={
        "User-Agent": bpv._UA, "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code})
    raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
    s, e = raw.find("{"), raw.rfind("}") + 1
    j = json.loads(raw[s:e])
    return {r["FSRQ"]: float(r["LJJZ"]) for r in j.get("Data", {}).get("LSJZList", []) if r.get("LJJZ")}


IDXS = {"932158": None, "932160": None, "932163": None, "950245": None}
for c in IDXS:
    IDXS[c] = idx_perf(c)
    s = sorted(IDXS[c])
    print("  %-8s %-34s %2d 条 [%s ~ %s]"
          % (c, next(iter(IDXS[c].values()))["name"] if IDXS[c] else "-",
             len(s), s[0] if s else "-", s[-1] if s else "-"))

print()
print("=" * 100)
print("逐只对比：哪个指数拟合最好（误差绝对值中位 + 均方根 + 最大）")
print("=" * 100)
CODES = ["511070", "511110", "511190", "511200",
         "551900", "551500", "551550", "159600", "551030"]
best = {}
for code in CODES:
    nj = nav_cum(code)
    print("\n  【%s】净值 %d 条" % (code, len(nj)))
    scores = []
    for ic, ix in IDXS.items():
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
        scores.append((ae[len(ae) // 2], rmse, ae[-1], ic, len(errs),
                       statistics.median(errs)))
    scores.sort()
    for med, rmse, mx, ic, n, bias in scores:
        mark = " ★" if (scores and ic == scores[0][3]) else ""
        print("     %-8s n=%-3d |误差|中位=%6.3f  RMSE=%6.3f  max=%7.3f  偏差=%+6.3f%s"
              % (ic, n, med, rmse, mx, bias, mark))
    if scores:
        best[code] = scores[0][3]

print()
print("=" * 100)
print("定案：代码 → 跟踪指数")
print("=" * 100)
NAME = {"950245": "上证基准做市公司债指数",
        "932158": "中证科技创新公司债指数",
        "932160": "中证AAA科技创新公司债指数",
        "932163": "中证AAA国企科技创新公司债指数"}
for c in CODES:
    print("  %-7s → %-8s %s" % (c, best.get(c, "?"), NAME.get(best.get(c), "")))
