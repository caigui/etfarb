# -*- coding: utf-8 -*-
"""只读探测 9：用项目已有的 IndexQuoteProvider 试 950245 / AAA科创债指数。

重点：`2.950245` 这种 secid 能否取到？中证 AAA 科创债指数的真实代码是什么？
"""
import io, sys, os, json, time, urllib.request, ssl
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def try_secid(secid):
    """东财 push2delay / push2 双通道试 secid。"""
    out = {}
    for host in ("push2delay.eastmoney.com", "push2.eastmoney.com"):
        url = ("https://%s/api/qt/stock/get?secid=%s"
               "&fields=f43,f57,f58,f60,f170&invt=2&fltt=2&_=%d"
               % (host, secid, int(time.time() * 1000)))
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"})
            raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8")
            j = json.loads(raw)
            d = j.get("data")
            out[host] = (("%s(%s) f43=%s f60=%s f170=%s"
                          % (d.get("f58"), d.get("f57"), d.get("f43"), d.get("f60"), d.get("f170")))
                         if d else "data=null")
        except Exception as e:
            out[host] = "ERR %s %s" % (type(e).__name__, str(e)[:50])
    return out


print("=" * 100)
print("A  中证指数 secid 试探（950245 沪做市公司债 / AAA科创债候选码）")
print("=" * 100)
CAND = [
    ("950245", "沪做市公司债"),
    ("932158", "中证AAA科创债(猜1)"),
    ("950050", "上证AAA科创债(猜)"),
    ("932063", "中证AAA科创债(猜2)"),
    ("H11001", "中证全债"),
    ("H11008", "中证企业债"),
    ("CBA00101", "中债国债总财富"),
    ("931917", "中证0-3年AAA公司债(猜)"),
]
for code, nm in CAND:
    print("\n  -- %s %s --" % (code, nm))
    for pre in ("2.", "1.", "0.", "100."):
        r = try_secid(pre + code)
        for host, v in r.items():
            if "data=null" not in v and "ERR" not in v:
                print("     %-4s %-26s ✅ %s" % (pre, host, v))
    print("     (已试 2./1./0./100. 无输出=全部 data=null)")

print()
print("=" * 100)
print("B  用项目 IndexQuoteProvider（支持 .CSI 归一化）")
print("=" * 100)
import index_quote as iq

p = iq.IndexQuoteProvider()
for t in ("950245.CSI", "950245", "932158.CSI", "950050.CSI",
          "H11001.CSI", "H11008.CSI", "511220", "511070"):
    try:
        print("  %-14s -> %s" % (t, p.get_change(t)))
    except Exception as e:
        print("  %-14s ❌ %s %s" % (t, type(e).__name__, str(e)[:70]))

print()
print("=" * 100)
print("C  中证官网指数详情页（含真实代码）")
print("=" * 100)
import re
def _txt(url, referer=None, timeout=15):
    h = {"User-Agent": _UA}
    if referer:
        h["Referer"] = referer
    return urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                  timeout=timeout, context=_CTX).read().decode("utf-8", "ignore")

probes = [
    ("950245 详情", "https://www.csindex.com.cn/en/indices/index-detail/950245"),
    ("950245 中文", "https://www.csindex.com.cn/#/indices/family/detail?indexCode=950245"),
    ("中证指数 API v1", "https://www.csindex.com.cn/csindex-home/indexInfo/index-basic-info?indexCode=950245"),
]
for nm, u in probes:
    try:
        t = _txt(u, referer="https://www.csindex.com.cn/")
        print("  %-16s len=%-6d %s" % (nm, len(t), t[:180].replace("\n", " ")))
    except Exception as e:
        print("  %-16s ❌ %s %s" % (nm, type(e).__name__, str(e)[:70]))
