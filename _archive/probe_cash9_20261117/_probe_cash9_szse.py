# -*- coding: utf-8 -*-
"""只读探测 4：深市 159600 PCF 通道 + 沪市成份券的替代价格源。

重点：
 A. pcf_fetcher.fetch_pcf('159600', date) 能否取到
 B. 沪市交易所信用债是否有「收盘价」历史（新浪/东财 K 线）—— 有收盘价就能逐券算 ΔP
 C. 中证指数官网真实指数代码查询
 D. 511070 等 ETF 自身的 IOPV（上交所是否发布债券ETF的IOPV）
"""
import io, sys, os, json, re, ssl, urllib.request
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
    return urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                  timeout=timeout, context=_CTX).read()


def _txt(url, referer=None, timeout=15):
    return _raw(url, referer, timeout).decode("utf-8", "ignore")


print("=" * 100)
print("A  深市 159600 PCF（pcf_fetcher 通道）")
print("=" * 100)
import pcf_fetcher as pf

for d in [date.today().isoformat(),
          (date.today() - timedelta(days=1)).isoformat(),
          (date.today() - timedelta(days=3)).isoformat()]:
    d8 = d.replace("-", "")
    try:
        t, src = pf.fetch_pcf("159600", d8)
        print("  %s -> %s  len=%s" % (d8, src, len(t) if t else 0))
        if t:
            print("  前 500 字:\n    " + t[:500].replace("\n", "\n    "))
            print("  是否像 PCF:", pf._looks_like_pcf(t))
            break
    except Exception as e:
        print("  %s ❌ %s %s" % (d8, type(e).__name__, str(e)[:100]))

print()
print("=" * 100)
print("B  沪市交易所信用债收盘价历史（新浪 / 东财 K 线）")
print("=" * 100)
SAMPLE = ["115323", "240085", "137851"]
for c in SAMPLE:
    print("\n  -- %s --" % c)
    # B1 新浪日K
    try:
        u = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "CN_MarketData.getKLineData?symbol=sh%s&scale=240&ma=no&datalen=6" % c)
        t = _txt(u, referer="https://finance.sina.com.cn/")
        print("    新浪日K: %s" % t[:260])
    except Exception as e:
        print("    新浪日K ❌ %s %s" % (type(e).__name__, str(e)[:90]))
    # B2 东财 K 线
    try:
        u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
             "secid=1.%s&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
             "&klt=101&fqt=0&end=20500101&lmt=6" % c)
        j = json.loads(_txt(u, referer="https://quote.eastmoney.com/"))
        d = j.get("data")
        print("    东财K线: %s" % (json.dumps(d, ensure_ascii=False)[:300] if d else "None"))
    except Exception as e:
        print("    东财K线 ❌ %s %s" % (type(e).__name__, str(e)[:90]))

print()
print("=" * 100)
print("C  中证指数代码查询（950245 沪做市公司债 / AAA科创债）")
print("=" * 100)
for q in ("950245", "AAA科创债", "沪做市公司债"):
    try:
        u = ("https://www.csindex.com.cn/csindex-home/search/search-index?"
             "searchInput=%s&pageNum=1&pageSize=5&dataType=1" % q)
        t = _txt(u, referer="https://www.csindex.com.cn/")
        print("  [%s] -> %s" % (q, t[:300].replace("\n", " ")))
    except Exception as e:
        print("  [%s] ❌ %s %s" % (q, type(e).__name__, str(e)[:90]))

print()
print("=" * 100)
print("D  9 只 ETF 的交易所行情完整性（前收/现价/涨跌幅）")
print("=" * 100)
CODES = ["511070", "511110", "511190", "511200", "551900", "551030",
         "551500", "551550", "159600"]
for c in CODES:
    pre = "sh" if c[0] == "5" else "sz"
    try:
        t = _txt("https://hq.sinajs.cn/list=%s%s" % (pre, c),
                 referer="https://finance.sina.com.cn/")
        body = t.split('"')[1] if '"' in t else ""
        f = body.split(",")
        if len(f) > 5:
            print("  %-7s %-14s 开=%-8s 昨收=%-8s 现价=%-8s 高=%-8s 低=%-8s 量=%s"
                  % (c, f[0], f[1], f[2], f[3], f[4], f[5], f[8] if len(f) > 8 else "-"))
        else:
            print("  %-7s 字段不足: %s" % (c, body[:100]))
    except Exception as e:
        print("  %-7s ❌ %s %s" % (c, type(e).__name__, str(e)[:80]))
