# -*- coding: utf-8 -*-
"""只读探测 2：成份券的交易所行情源 + 跟踪指数行情 + 深市 159600 PCF。

要回答三个问题：
  Q1. 沪市成份券（115323 等）能否从免费源取到实时/当日净价与涨跌？
  Q2. 跟踪指数（950245 沪做市公司债 / 中证AAA科创债 / 上证AAA科创债）行情能否取到？
  Q3. 159600 深市 PCF 通道是否存在？
"""
import io, sys, os, json, re, ssl, urllib.request, traceback
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _get(url, referer=None, timeout=12):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=timeout, context=_CTX).read()


def _txt(url, **kw):
    return _get(url, **kw).decode("utf-8", "ignore")


SAMPLE = ["115323", "115474", "115549", "240085", "137851", "163112"]


def q1_exchange_bond_quote():
    print("=" * 100)
    print("Q1  交易所成份券行情（样本 %s）" % SAMPLE)
    print("=" * 100)

    # 1-a 新浪 sh+代码
    print("\n[1-a] 新浪 hq.sinajs.cn  list=sh115323,sh240085,...")
    try:
        syms = ",".join("sh" + c for c in SAMPLE)
        t = _txt("https://hq.sinajs.cn/list=" + syms,
                 referer="https://finance.sina.com.cn/")
        print("  原始输出（截断 600 字）:")
        print("  " + t.replace("\n", "\n  ")[:600])
    except Exception as e:
        print("  ❌", type(e).__name__, str(e)[:120])

    # 1-b 东财 push2
    print("\n[1-b] 东财 push2.eastmoney.com（secid=1.代码 沪 / 0.代码 深）")
    for c in SAMPLE[:4]:
        secid = ("1." if c[0] in "0123" and len(c) == 6 and c[0] in "0123456789" else "1.") + c
        url = ("https://push2.eastmoney.com/api/qt/stock/get?"
               "secid=%s&fields=f43,f57,f58,f60,f170,f169,f46,f44,f45,f47,f48" % secid)
        try:
            j = json.loads(_txt(url, referer="https://quote.eastmoney.com/"))
            d = j.get("data")
            print("  %-8s -> %s" % (c, ("无数据(None)" if not d else
                  "name=%s price=%s preclose=%s chg%%=%s" %
                  (d.get("f58"), d.get("f43"), d.get("f60"), d.get("f170")))))
        except Exception as e:
            print("  %-8s ❌ %s %s" % (c, type(e).__name__, str(e)[:80]))

    # 1-c 上交所固收 / 债券行情
    print("\n[1-c] 上交所 query.sse.com.cn 债券行情（试 3 个已知 sqlId）")
    cands = [
        ("COMMON_SSE_ZQPZ_GPLB_MCJS_GZLB_L",
         "http://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_GPLB_MCJS_GZLB_L"
         "&bondCode=115323"),
        ("bondTradeData",
         "http://query.sse.com.cn/marketdata/tradedata/queryTradeData.do"
         "&productId=115323&reportType=DAY"),
        ("BOND_DEAL",
         "http://query.sse.com.cn/commonSoaQuery.do?sqlId=BOND_DEAL&bondCode=115323"),
    ]
    for nm, u in cands:
        try:
            t = _txt(u, referer="https://www.sse.com.cn/")
            print("  %-34s -> %s" % (nm, t[:200].replace("\n", " ")))
        except Exception as e:
            print("  %-34s ❌ %s %s" % (nm, type(e).__name__, str(e)[:80]))


def q2_index_quote():
    print()
    print("=" * 100)
    print("Q2  跟踪指数行情（950245 沪做市公司债 / 932158? 中证AAA科创债）")
    print("=" * 100)
    IDXS = [("950245", "沪做市公司债"), ("932158", "中证AAA科创债(猜)"),
            ("950050", "上证AAA科创债(猜)")]
    for code, nm in IDXS:
        # 2-a 中证官网
        u = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=%s" % code)
        try:
            t = _txt(u, referer="https://www.csindex.com.cn/")
            j = json.loads(t)
            d = j.get("data") or {}
            print("  [中证] %-6s %-18s -> %s" % (code, nm, json.dumps(d, ensure_ascii=False)[:220]))
        except Exception as e:
            print("  [中证] %-6s %-18s ❌ %s %s" % (code, nm, type(e).__name__, str(e)[:80]))
        # 2-b 东财指数
        u2 = ("https://push2.eastmoney.com/api/qt/stock/get?secid=2.%s"
              "&fields=f43,f57,f58,f60,f170,f169" % code)
        try:
            j = json.loads(_txt(u2, referer="https://quote.eastmoney.com/"))
            print("  [东财] %-6s %-18s -> %s" % (code, nm, json.dumps(j.get("data"), ensure_ascii=False)[:200]))
        except Exception as e:
            print("  [东财] %-6s %-18s ❌ %s %s" % (code, nm, type(e).__name__, str(e)[:80]))


def q3_szse_pcf():
    print()
    print("=" * 100)
    print("Q3  深市 159600 科创债ETF嘉实 PCF 通道")
    print("=" * 100)
    urls = [
        ("深交所 PCF 文件（szse 静态宽表）",
         "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
         "&CATALOGID=1945_LOF&txtQueryKeyAndJC=159600&TABKEY=tab1"),
        ("深交所 ETF 清单",
         "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
         "&CATALOGID=1815_stock&txtQueryKeyAndJC=159600&TABKEY=tab1"),
        ("嘉实官网 PCF（试常见路径）",
         "https://www.jsfund.cn/etf/pcf/159600.xml"),
    ]
    for nm, u in urls:
        try:
            t = _txt(u, referer="https://www.szse.cn/")
            print("  %-32s -> %s" % (nm, t[:300].replace("\n", " ")))
        except Exception as e:
            print("  %-32s ❌ %s %s" % (nm, type(e).__name__, str(e)[:100]))


def q4_etf_iopv():
    print()
    print("=" * 100)
    print("Q4  ETF 本身实时价 / 官方 IOPV（上交所债券 ETF 是否发布 IOPV）")
    print("=" * 100)
    CODES = ["511070", "551900", "159600"]
    for c in CODES:
        pre = "sh" if c[0] == "5" else "sz"
        try:
            t = _txt("https://hq.sinajs.cn/list=%s%s" % (pre, c),
                     referer="https://finance.sina.com.cn/")
            print("  %-6s %s" % (c, t.strip()[:260]))
        except Exception as e:
            print("  %-6s ❌ %s %s" % (c, type(e).__name__, str(e)[:80]))


if __name__ == "__main__":
    for fn in (q1_exchange_bond_quote, q2_index_quote, q3_szse_pcf, q4_etf_iopv):
        try:
            fn()
        except Exception:
            traceback.print_exc()
