# -*- coding: utf-8 -*-
"""
bond_cash_etf_valuation.py —— 现金管理类「交易所信用债 ETF」逐日估值
================================================================================

标的（2026-11-17 东哥要求新增 9 只，原为 pending 无估值）：
  沪市·基准做市公司债  511070 公司债ETF南方 / 511110 公司债ETF易方达
                       511190 信用债ETF海富通 / 511200 信用债ETF华夏
  沪市·AAA 科创债      551900 科创债ETF招商 / 551030 科创债ETF鹏华
                       551500 科创债ETF易方达 / 551550 科创债ETF华夏
  深市·AAA 科创债      159600 科创债ETF嘉实

【为什么不能用「逐券真实净价」】
--------------------------------------------------
这批 ETF 的成份券全是**交易所信用债**（115xxx / 240xxx / 137xxx 等）。
实测三条逐券路径全部不可用（2026-11-17，逐条留证）：

  ① 交易所实时行情（新浪 / 东财 push2 / 上交所固收）
     全部返回 **0.000**。原因：这些券在交易所几乎无连续成交（115323 近一年
     仅 4 个交易日有成交记录），行情源不推送。→ 取不到净价。

  ② CFETS 逐券估值（bond_pcf_valuation.fetch_cfets_valuation）
     全表仅 252 条，**只覆盖政金债/国债**，对 313 只成份券命中率 **0.0%**。
     → 一条都取不到。

  ③ 货币网银行间现券成交（fetch_spot_deal）
     这是**银行间**市场。这批券是交易所券，名称含「公司债 / 科创债 / AAA」
     的命中数为 0。银行间中票(MTN)虽有 51 条，但 2026-11-17 实测 Δy 中位数
     **+7bp（利率上行）**，而当日 9 只 ETF 全部涨 0.3~0.67% —— 方向完全相反，
     样本里大量中低评级新券，与 AAA 短久期组合不是一回事。→ 方向会算反。

【最终方案：中证指数官方日涨跌】
--------------------------------------------------
中证指数官网对这批 ETF 的跟踪指数提供**日收盘点位与涨跌幅**：

    GET https://www.csindex.com.cn/csindex-home/perf/index-perf
        ?indexCode=<idx>&startDate=<YYYYMMDD>&endDate=<YYYYMMDD>
    → data[].{tradeDate, close, change, changePct, consNumber}

实测精度（2026-10-20~11-16，19 个交易日，逐日 vs 官方累计净值涨跌）：

    代码      跟踪指数  指数名称                        |误差|中位   P90     max
    511070   950245   上证基准做市公司债指数            0.33bp   1.38bp  1.58bp
    511110   950245   上证基准做市公司债指数            0.89bp   1.49bp  3.56bp
    511190   950245   上证基准做市公司债指数            0.57bp   2.26bp  2.35bp
    511200   950245   上证基准做市公司债指数            0.44bp   2.30bp  2.61bp
    551900   932158   中证科技创新公司债指数            0.30bp   1.05bp  2.11bp
    551500   932158   中证科技创新公司债指数            0.36bp   1.29bp  1.42bp
    551550   932158   中证科技创新公司债指数            0.07bp   0.83bp  2.11bp
    159600   932158   中证科技创新公司债指数            0.84bp   2.10bp  2.13bp
    551030   932160   中证AAA科技创新公司债指数         0.34bp      —    2.12bp

  ⇒ 指数日涨跌几乎是净值的完美代理（|误差|中位 < 1bp）。
     对照：兄弟 ETF 价格代理 |误差|中位 3.4~5.2bp、P90 达 25~36bp、max 88bp，
     因为**折溢价日间波动**被混进了净值（这批 ETF 折价可达 -0.77%），
     所以「用别的 ETF 价格推净值」不可行，必须用指数。

【两级精度：盘中 vs 收盘后】
--------------------------------------------------
中证指数官网的 perf 接口**只在收盘后发布当日数据**（实测盘中最新一条为 T-1）。
因此：

  ① 收盘后（当日数据已出）→ `csi_index_daily`
     est = nav(T-1) × (1 + 指数涨跌%/100)
     精度 ≈ ±1bp（上表），这是本模块的**主口径**。

  ② 盘中（当日指数未发布）→ `peers_implied_intraday`
     取同指数兄弟 ETF 的**盘中涨跌幅中位数**，乘本只自己的锚点。
     本质是「假设今日折溢价与昨日相同」，
     折溢价一旦日间变动就会带来误差（历史 P90 约 25~36bp）。
     ⇒ 明确标注为**盘中粗估**，不能当精确值用，但方向与量级可用。

  ③ 都不可得 → 退回纯票息（`carry_only`），est 只含票息，方向=0。

【🔴 票息（carry）不能叠加 —— 这是本模块最容易写错的一点】
--------------------------------------------------
中证债券指数（950245 / 932158 / 932160）是**财富指数**口径，日涨跌里已经含
当日票息；ETF 二级市场价格的日涨跌同样含票息（价格随净值上行）。
再叠一次 carry 就是**重复计息**。

19 个交易日实测（vs 累计净值涨跌）：

    口径          |误差|中位            均值偏差
    不叠 carry   0.30 ~ 0.84bp        -0.08 ~ +0.21bp    ✅
    叠 carry     0.98 ~ 1.93bp        +0.98 ~ +1.85bp    ❌ 单向高估

即日票息（约 1bp）被算了两次。故 ①②③ 三条主口径**一律不叠 carry**；
carry 只保留给 ④「无任何方向信号」的兜底 —— 那时它是唯一的增量来源。

【锚点与分红】
--------------------------------------------------
锚点用东财 lsjz 的**累计净值（LJJZ）口径换算**，避免分红造成假跌幅：
实测 551500 于 2026-11-16 每 10 份派 16 元，单位净值 102.6844→100.9703
（-1.669%），但累计净值仅 -0.107%。若用单位净值算日涨跌会误判为暴跌。
本模块的 `_nav_series` 同时返回单位净值（展示）与累计净值（算涨跌），
`_cum_ratio` 优先用累计净值。

盘中兄弟反推时另设**除权日守卫**：分红当天本只价格跌幅天然比兄弟大 50bp 以上，
若照用兄弟中位数会把派现误判成净值下跌，故本只涨幅与兄弟中位数相差 >60bp
时改用本只自身价格涨幅（阈值 60bp：最低派现率也在 50bp 以上，而深市流动性
噪音实测可达 27bp，取 35bp 会误触发）。

【🔴 日期对齐（防双重计算）】
--------------------------------------------------
若指数最新发布日 <= 锚点净值日，说明那段行情**已被官方净值消化**，
绝不能再叠一次（实测不判这条，551030 会凭空多跌 12bp）。
551030 的代理指数分支尤其依赖此判据。

【篮子校验（PCF）】
--------------------------------------------------
上交所/深交所 PCF 公告的 NAV 字段 = T-1 官方净值（实测 511070：
PCF NAV 103.1195 与东财 11-16 净值完全一致），可作锚点一致性校验，
偏差 >5bp 时在 detail 里提示「锚点可能未同步」。
"""

from __future__ import print_function

import json
import logging
import os
import re
import ssl
import time
import urllib.request
from datetime import date, datetime, timedelta

import bond_pcf_valuation as bpv          # _req / 交易日工具 / xlsx 解析
import bond_etf_valuation as _bev         # 净值缓存 / 新浪K线 / 活跃合约

logger = logging.getLogger("bond_cash_etf_valuation")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data", "bond_cash_etf")

ALGO_ID = "cash_bond_etf_csi_index_v1"
ALGO_NAME = "现金债ETF·中证指数日涨跌锚定"
ALGO_VERSION = "2026-11-17"

# ── 标的元信息 ──
#  idx   : 中证指数代码（实测最佳拟合，见模块 docstring 精度表）
#  peers : 跟踪同一指数的兄弟 ETF（盘中反推用，**不含自己**）
CASH_BOND_ETF_META = {
    # —— 沪市·基准做市公司债（跟踪 950245 上证基准做市公司债指数）——
    "511070": {"name": "公司债ETF南方", "idx": "950245",
               "idx_name": "上证基准做市公司债指数",
               "peers": ["511110", "511190", "511200"]},
    "511110": {"name": "公司债ETF易方达", "idx": "950245",
               "idx_name": "上证基准做市公司债指数",
               "peers": ["511070", "511190", "511200"]},
    "511190": {"name": "信用债ETF海富通", "idx": "950245",
               "idx_name": "上证基准做市公司债指数",
               "peers": ["511070", "511110", "511200"]},
    "511200": {"name": "信用债ETF华夏", "idx": "950245",
               "idx_name": "上证基准做市公司债指数",
               "peers": ["511070", "511110", "511190"]},
    # —— 沪市·AAA 科创债（跟踪 932158 中证科技创新公司债指数）——
    "551900": {"name": "科创债ETF招商", "idx": "932158",
               "idx_name": "中证科技创新公司债指数",
               "peers": ["551500", "551550", "159600"]},
    "551500": {"name": "科创债ETF易方达", "idx": "932158",
               "idx_name": "中证科技创新公司债指数",
               "peers": ["551900", "551550", "159600"]},
    "551550": {"name": "科创债ETF华夏", "idx": "932158",
               "idx_name": "中证科技创新公司债指数",
               "peers": ["551900", "551500", "159600"]},
    "159600": {"name": "科创债ETF嘉实", "idx": "932158",
               "idx_name": "中证科技创新公司债指数",
               "peers": ["551900", "551500", "551550"]},
    # —— 沪市·上证 AAA 科创债（实测最佳拟合 932160，误差 0.34bp）——
    #   ⚠️ 551030 是这批里**唯一无同指数兄弟 ETF**的（551900/551500/551550/159600
    #   跟的是 932158）。两层兜底：
    #     · idx_proxy = 932158：收盘后 932160 尚未发布时，用同族指数的日涨跌代理
    #       （实测两只指数日涨跌差 |中位| 0.00bp、max 1.00bp，回测 0.72bp）。
    #     · peers = 932158 的四只 ETF：盘中连代理指数都没有（中证不发盘中），
    #       就用它们的盘中涨幅中位数间接代理 —— 同样建立在两指数几乎同步的前提上。
    #   若不这样处理，551030 盘中只能退化为纯票息（方向=0），
    #   而当天同类 ETF 普遍涨 0.55%，等于白丢一个可用信号。
    "551030": {"name": "科创债ETF鹏华", "idx": "932160",
               "idx_name": "中证AAA科技创新公司债指数",
               "idx_proxy": "932158",
               "peers": ["551900", "551500", "551550", "159600"],
               "peers_are_proxy": True},
}

CASH_BOND_ETF_CODES = tuple(CASH_BOND_ETF_META)

_CSI_PERF_URL = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
                 "?indexCode=%s&startDate=%s&endDate=%s")
_CSI_REF = "https://www.csindex.com.cn/"

# 中证 perf 单次查询窗口实测上限约 45 天（200/100 天均返回空 data）
CSI_MAX_SPAN = 45
CSI_TTL = 900              # 指数日线缓存 15 分钟（收盘后才更新，无需更短）
PEERS_TTL = 25             # 兄弟 ETF 盘中价缓存 25 秒（与看板刷新同量级）

_idx_cache = {}            # {(idx, day8): (monotonic, data)}
_idx_series_cache = {}     # {(idx, span): (monotonic, {date: {...}})}
_peers_cache = {"t": 0.0, "v": None}
# 🔴 2026-11-21：净值序列缓存。peers_implied_nav 会对每个兄弟 ETF 调 _nav_series，
#    而 _nav_series 每次发**两次**东财请求（DWJZ + LJJZ）。9 只标的 × ~9 兄弟
#    ≈ 80 次串行网络请求，是周末债券模块 build 冷启动慢到 3 分钟的元凶。
#    净值是日频数据，300s TTL 足够（盘中不会变），重复调用直接命中。
_NAV_SERIES_TTL = 300
_nav_series_cache = {}     # {(code, days): (monotonic, [(date, dwjz, ljjz, fhsp)])}
_state = {}                # {code: {"ok": bool, "msg": str}} 状态化告警


def clear_caches():
    """清本模块进程内缓存（看板「补采+重算」用）。"""
    _idx_cache.clear()
    _idx_series_cache.clear()
    _peers_cache.update({"t": 0.0, "v": None})
    _nav_series_cache.clear()
    _state.clear()


def _log_state(code, ok, msg=None):
    """状态切换才写日志，避免看板 30 秒一刷把同一条 WARNING 刷满屏。"""
    st = _state.setdefault(code, {"ok": None})
    prev = st.get("ok")
    st["ok"] = ok
    if ok:
        if prev is False:
            logger.info("[cash_etf] %s 估值已恢复", code)
        elif prev is None:
            logger.info("[cash_etf] %s 估值就绪", code)
        return
    st["msg"] = msg
    if prev is not False:
        logger.warning("[cash_etf] %s 估值降级：%s", code, msg)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _d8(s):
    return re.sub(r"\D", "", str(s or ""))[:8]


def _iso(d8):
    s = _d8(d8)
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8]) if len(s) == 8 else ""


# ════════════════════ 中证指数日线（主驱动） ════════════════════

def fetch_csi_series(idx_code, days=CSI_MAX_SPAN, force=False):
    """中证指数日线 {YYYY-MM-DD: {close, pct, chg, cons, name}}。

    走 bond_pcf_valuation._req（curl_cffi 指纹降级 + 代理兜底），失败返回 {}。
    按 (idx, span) 缓存 CSI_TTL 秒；**只在取到数据时写缓存**，
    避免一次限流/风控把空结果污染 15 分钟（同 bond_etf_valuation 的 K 线策略）。
    """
    span = min(int(days or CSI_MAX_SPAN), CSI_MAX_SPAN)
    key = (str(idx_code), span)
    now = time.monotonic()
    if not force:
        hit = _idx_series_cache.get(key)
        if hit and now - hit[0] < CSI_TTL:
            return hit[1]

    start = (date.today() - timedelta(days=span)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    url = _CSI_PERF_URL % (idx_code, start, end)
    out = {}
    err = None
    for attempt in range(3):
        try:
            r = bpv._req("get", url, timeout=20,
                         headers={"User-Agent": bpv._UA, "Referer": _CSI_REF})
            body = getattr(r, "text", None)
            if body is None:
                body = getattr(r, "content", b"").decode("utf-8", "ignore")
            j = json.loads(body)
            for row in j.get("data") or []:
                d = _iso(row.get("tradeDate"))
                if not d:
                    continue
                out[d] = {"close": _num(row.get("close")),
                          "pct": _num(row.get("changePct")),
                          "chg": _num(row.get("change")),
                          "cons": _num(row.get("consNumber")),
                          "name": row.get("indexNameCnAll")}
            if out:
                break
            err = "data 为空（可能被风控/窗口超限）"
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, str(e)[:100])
        # 中证 WAF 对密集请求敏感：退避 1.5s / 3s
        time.sleep(1.5 * (attempt + 1))

    if out:
        _idx_series_cache[key] = (now, out)
        # 落盘一份日线，供历史回看与离线核对（失败不影响主流程）
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            fp = os.path.join(DATA_DIR, "csi_%s_%s.json" % (idx_code,
                                                            date.today().strftime("%Y%m%d")))
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.debug("[cash_etf] %s 指数日线落盘失败: %s", idx_code, e)
    elif err:
        logger.debug("[cash_etf] %s 指数取数失败: %s", idx_code, err)
    return out


def csi_pct_on(idx_code, day):
    """指数在指定日的涨跌幅%（取不到返回 None）。

    day 为 None/今天时，若当日尚未发布（盘中）→ 返回 None，
    由上层转「兄弟 ETF 反推」分支。同时给出**最近可得日**用于兜底展示。
    """
    series = fetch_csi_series(idx_code)
    if not series:
        return None, None
    d = str(day or date.today())[:10]
    if d in series and series[d].get("pct") is not None:
        return series[d]["pct"], d
    return None, None


def csi_series_long(idx_code, segments=4, span=CSI_MAX_SPAN, force=False):
    """分段把 perf 接口拼成**长历史**日线（每段 ≤45 天，向前回溯）。

    🔴 为什么需要这个：单次查询上限 45 天，而「滚动 beta 回归」需要 40+ 个
       交易日样本。45 天窗口按自然日算只覆盖约 30 个交易日，样本不够。
       分段连续请求实测可行（每段独立 45 天，间隔 1.2s 避开 WAF）。

    返回 {date: {close, pct, chg, cons, name}}，语义同 fetch_csi_series。
    """
    key = ("LONG", str(idx_code), int(segments), int(span))
    now = time.monotonic()
    if not force:
        hit = _idx_series_cache.get(key)
        if hit and now - hit[0] < CSI_TTL:
            return hit[1]

    out = {}
    today = date.today()
    for k in range(int(segments)):
        end = today - timedelta(days=int(span) * k)
        start = end - timedelta(days=int(span) - 1)
        url = _CSI_PERF_URL % (idx_code, start.strftime("%Y%m%d"),
                               end.strftime("%Y%m%d"))
        got = 0
        for attempt in range(3):
            try:
                r = bpv._req("get", url, timeout=20,
                             headers={"User-Agent": bpv._UA, "Referer": _CSI_REF})
                body = getattr(r, "text", None)
                if body is None:
                    body = getattr(r, "content", b"").decode("utf-8", "ignore")
                j = json.loads(body)
                for row in j.get("data") or []:
                    d = _iso(row.get("tradeDate"))
                    if not d:
                        continue
                    out[d] = {"close": _num(row.get("close")),
                              "pct": _num(row.get("changePct")),
                              "chg": _num(row.get("change")),
                              "cons": _num(row.get("consNumber")),
                              "name": row.get("indexNameCnAll")}
                    got += 1
                if got:
                    break
            except Exception as e:
                logger.debug("[cash_etf] %s 长历史段%d失败: %s", idx_code, k, e)
            time.sleep(1.2 * (attempt + 1))
        time.sleep(1.2)          # 段间隔，避开中证 WAF

    if out:
        _idx_series_cache[key] = (now, out)
    return out


def csi_latest(idx_code):
    """指数最近一个已发布交易日 (date, pct, close, cons, name)。"""
    series = fetch_csi_series(idx_code)
    if not series:
        return None
    ds = sorted(series)
    for d in reversed(ds):
        v = series[d]
        if v.get("pct") is not None:
            return d, v
    return None


# ════════════════════ 净值序列（锚点） ════════════════════

def _nav_series(code, days=40):
    """东财 lsjz → [(date, 单位净值, 累计净值)] **降序**。

    🔴 必须同时取 LJJZ：551500 于 2026-11-16 每 10 份派 16 元，单位净值
       102.6844→100.9703（-1.669%），累计净值仅 -0.107%。只用 DWJZ 会把
       分红日误判成暴跌，估值锚点直接错 1.6 元/份。

    🔴 2026-11-21：加进程内 TTL 缓存 —— peers_implied_nav 对每个兄弟都调它，
       原来每次发两次东财请求，N+1 串行是周末 build 冷启动慢的主因。
    """
    import time as _time
    key = (str(code), int(days))
    now = _time.monotonic()
    hit = _nav_series_cache.get(key)
    if hit and now - hit[0] < _NAV_SERIES_TTL:
        return hit[1]
    try:
        recs = _bev._fetch_nav_history(code, days)
    except Exception:
        recs = []
    if not recs:
        _nav_series_cache[key] = (now, [])
        return []
    # _bev._fetch_nav_history 只回 (date, DWJZ)，这里补一次带 LJJZ 的请求
    out = []
    try:
        url = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
               "&pageIndex=1&pageSize=%d" % (code, days))
        r = bpv._req("get", url, timeout=15, headers={
            "User-Agent": bpv._UA,
            "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code})
        body = getattr(r, "text", None)
        if body is None:
            body = getattr(r, "content", b"").decode("utf-8", "ignore")
        s, e = body.find("{"), body.rfind("}") + 1
        j = json.loads(body[s:e])
        for x in j.get("Data", {}).get("LSJZList", []):
            d, dwjz, ljjz = x.get("FSRQ"), _num(x.get("DWJZ")), _num(x.get("LJJZ"))
            if d and dwjz:
                out.append((d, dwjz, ljjz, (x.get("FHSP") or "").strip()))
    except Exception as e:
        logger.debug("[cash_etf] %s 带累计净值的净值请求失败，退化用 DWJZ: %s", code, e)
    if not out:
        out = [(d, v, None, "") for d, v in recs]
    _nav_series_cache[key] = (now, out)
    return out


def _cum_ratio(code, d0, d1):
    """d0→d1 的**累计净值**涨跌倍数（剔除分红）。取不到返回 None。

    优先 LJJZ；缺失时用 DWJZ，但若该区间含分红（FHSP 非空）则判为不可用。
    """
    hist = {x[0]: x for x in _nav_series(code)}
    a, b = hist.get(d0), hist.get(d1)
    if not a or not b:
        return None
    if a[2] and b[2]:
        return b[2] / a[2]
    # 无累计净值：检查区间内是否有分红
    ds = sorted(d for d in hist if d0 <= d <= d1)
    if any(hist[d][3] for d in ds):
        return None
    if a[1] and b[1]:
        return b[1] / a[1]
    return None


# ════════════════════ 兄弟 ETF 盘中反推 ════════════════════

def _etf_quote(code):
    """ETF 盘中报价 {price, pre_close} 或 None（新浪 hq，gbk）。"""
    pfx = "sh" if str(code)[0] == "5" else "sz"
    try:
        req = urllib.request.Request(
            "https://hq.sinajs.cn/list=%s%s" % (pfx, code),
            headers={"User-Agent": bpv._UA,
                     "Referer": "https://finance.sina.com.cn/"})
        raw = urllib.request.urlopen(req, timeout=8, context=_CTX).read()
        txt = raw.decode("gb18030", "ignore")
        m = re.search(r'="([^"]*)"', txt)
        if not m:
            return None
        f = m.group(1).split(",")
        if len(f) < 4:
            return None
        px, pre = _num(f[3]), _num(f[2])
        if not px or not pre or px <= 0 or pre <= 0:
            return None
        return {"price": px, "pre_close": pre, "name": f[0]}
    except Exception:
        return None


def fetch_peers_quotes(codes, ttl=PEERS_TTL):
    """批量取 ETF 报价 {code: {price, pre_close}}，进程内缓存 ttl 秒。"""
    now = time.monotonic()
    if _peers_cache["v"] is not None and now - _peers_cache["t"] < ttl:
        return _peers_cache["v"]
    out = {}
    for c in codes:
        q = _etf_quote(c)
        if q:
            out[c] = q
    if out:
        _peers_cache.update({"t": now, "v": out})
    return out


def peers_implied_nav(code, quotes=None):
    """用同指数兄弟 ETF 的盘中价反推本只的**隐含涨跌幅**，再乘自己的锚点。

    原理：若今日折溢价与昨日相同，则「价格涨幅 = 净值涨幅」，于是
            本只隐含净值 = 本只昨净值 × (1 + 兄弟涨幅中位数)

    🔴 2026-11-17 两个必须避开的坑（实测踩过，务必保持此写法）：

      Bug A（绝对数中位数）——最初写成「取兄弟隐含净值的绝对数中位数」。
        但各 ETF 净值水平不同（511070 在 103.12、551900 在 102.10、159600 在
        101.90），绝对数中位数根本不是「本只」的净值。实测后果：511110
        算出 est=103.70 / 昨净值 101.37 → 假涨幅 +2.29%（真实约 +0.55%）。
        ⇒ 必须取**涨跌幅**中位数，再乘本只自己的锚点。

      Bug B（分红日假涨幅）——551500 于 2026-11-16 每份派 1.6 元，单位净值
        102.6844→100.9703（-1.669%），而它的锚点已是除息后净值 100.9703。
        若拿兄弟净值（102.x 量级）去比，会算出 +1.67% 的假涨幅。
        ⇒ Bug A 的修法同时解决此问题：涨幅是各兄弟用**自己的**昨收/昨昨净值
          算的，与「本只锚点是否除息」无关。而兄弟自身的 pre_close 与 nav0
          在分红日都已除息，两边同步，涨幅依然正确（实测 551500 作为 peer
          时给出 +0.559%，与实际一致）。

    返回 {pct, nav, n, detail} 或 None。
    ⚠️ 精度受「折溢价日间变动」限制，实测 P90 约 25~36bp，只作盘中粗估。
    """
    meta = CASH_BOND_ETF_META.get(code) or {}
    peers = meta.get("peers") or []
    if not peers:
        return None
    quotes = quotes if quotes is not None else fetch_peers_quotes(
        [code] + list(peers) + [p for m in CASH_BOND_ETF_META.values()
                                for p in (m.get("peers") or [])])
    pcts, det = [], []
    for p in peers:
        q = quotes.get(p)
        if not q:
            continue
        navs = _nav_series(p, days=8)
        if not navs:
            continue
        nd, nav0 = navs[0][0], navs[0][1]
        if not nav0 or not q["pre_close"]:
            continue
        pct = (q["price"] / q["pre_close"] - 1) * 100
        pcts.append(pct)
        det.append({"代码": p, "现价": q["price"], "昨收": q["pre_close"],
                    "昨净值": nav0, "净值日": nd,
                    "价格涨幅%": round(pct, 4),
                    "隐含净值": round(nav0 * (1 + pct / 100.0), 4)})
    if not pcts:
        return None
    pcts.sort()
    n = len(pcts)
    med = pcts[n // 2] if n % 2 else (pcts[n // 2 - 1] + pcts[n // 2]) / 2.0

    # ── 除权日守卫 ──
    # 分红除权当天，本只价格 = 昨收 − 每份派现 + 当日收益，跌幅天然比兄弟大；
    # 而兄弟的涨幅里没有这笔派现。若照用兄弟中位数，会把「派现」误判成「净值下跌」，
    # 少算整整一个分红额（551500 为 1.6 元/份 ≈ 1.57%）。
    # 反过来，此时**本只自己的价格涨幅**恰好等于 (当日收益 − 派现)/昨收，
    # 乘锚点后正是除息后的真实净值 ⇒ 偏离够大就改用本只价格涨幅。
    # 阈值取 60bp：这批 ETF 的派现率最低也在 50bp 以上（净值 ~102，最小派 0.5 元
    #   ≈ 49bp），而深市流动性差造成的折溢价噪音实测可达 27bp（159600 当日
    #   自身 +0.32% vs 兄弟 +0.59%）。35bp 会把这种噪音误判成除权，故放宽到 60bp。
    own = quotes.get(code)
    own_pct = None
    if own and own.get("pre_close"):
        own_pct = (own["price"] / own["pre_close"] - 1) * 100
    div_suspect = bool(own_pct is not None and abs(own_pct - med) > 0.60)
    if div_suspect:
        return {"pct": own_pct, "n": n, "detail": det,
                "own_pct": own_pct, "peers_pct": med, "div_suspect": True}
    return {"pct": med, "n": n, "detail": det,
            "own_pct": own_pct, "peers_pct": med, "div_suspect": False}


# ════════════════════ 票息（carry） ════════════════════

def _daily_carry_yield(code, days=20):
    """从历史净值反推「日均票息收益率」（小数，非百分数）。

    🔴 用途**仅限 ④ 纯票息兜底**（没有任何方向信号时才用）。主口径 ①②③ 一律
       不叠 carry —— 中证债券指数是财富指数、ETF 价格也随净值上行，两者涨幅
       本身已含票息，叠加会重复计息（实测 +1.0~1.9bp 单向高估）。

    这批 ETF 持仓是 2~3 年 AAA 信用债，年化票息约 1.8%~2.2%。
    用近 20 个交易日的累计净值日均增长做估计，并**剔除 |日涨跌| > 30bp
    的异常日**（通常是分红、大额申赎或指数调样），避免污染。
    """
    navs = _nav_series(code, days=days + 6)
    if len(navs) < 6:
        return None
    asc = list(reversed(navs))
    chgs = []
    for i in range(1, len(asc)):
        r = _cum_ratio(code, asc[i - 1][0], asc[i][0])
        if r is None:
            continue
        pct = (r - 1) * 100
        if abs(pct) > 0.30:          # >30bp 视为异常日
            continue
        chgs.append(pct)
    if len(chgs) < 4:
        return None
    # 中位数而非均值：票息是稳定项，中位数更能代表「无行情变动日」的增长
    chgs.sort()
    med = chgs[len(chgs) // 2] / 100.0
    # 夹在 [0, 8bp/日] 内：负值说明样本全是下跌日，票息无法从净值里分离 → 用兜底
    if med <= 0:
        return None
    return min(med, 0.0008)


# ════════════════════ 主入口 ════════════════════

def estimate_cash_bond_nav(code, target_date=None):
    """单只现金债 ETF 的日内估值。返回形状对齐 estimate_bond_nav：

    {est, idx_chg, index_nm, idx_price, method, note,
     latest_nav, nav_date, detail, algo_id, algo_name}
    """
    out = {"est": None, "idx_chg": None, "index_nm": None, "idx_price": None,
           "method": "cash_bond", "note": None,
           "latest_nav": None, "nav_date": None, "detail": {},
           "algo_id": ALGO_ID, "algo_name": ALGO_NAME}
    meta = CASH_BOND_ETF_META.get(code)
    if not meta:
        out["note"] = "未登记的现金债 ETF %s" % code
        return out

    navs = _nav_series(code)
    if not navs:
        out["note"] = "净值历史获取失败（天天基金）"
        _log_state(code, False, "净值历史获取失败")
        return out
    nav_date, latest_nav = navs[0][0], navs[0][1]
    out["latest_nav"] = latest_nav
    out["nav_date"] = nav_date
    out["index_nm"] = meta["idx_name"]

    target = str(target_date or date.today())[:10]
    # 目标日不晚于净值日 ⇒ 无更新行情，直接锚点（纯计算不重复叠加）
    if target <= nav_date:
        out["est"] = round(latest_nav, 4)
        out["method"] = "cash_bond_anchor_only"
        out["note"] = ("目标日 %s 不晚于最新官方净值日 %s，无更新行情；"
                       "显示锚点净值 %.4f" % (target, nav_date, latest_nav))
        out["detail"] = {"估值状态": "锚点净值（无更新行情）",
                         "锚点": {"日期": nav_date, "净值": latest_nav},
                         "算法": {"id": ALGO_ID, "version": ALGO_VERSION}}
        _log_state(code, True)
        return out

    carry = _daily_carry_yield(code)
    carry_amt = latest_nav * carry if carry else 0.0

    # 🔴 2026-11-17 第 4 个 bug：**carry 不能叠加**。
    #   中证债券指数（950245/932158/932160）是**财富指数**口径，日涨跌里已经含
    #   当日票息；ETF 二级市场价格的日涨跌同样含票息（价格随净值上行）。
    #   再叠一次 carry 就是重复计息。19 个交易日实测（vs 累计净值涨跌）：
    #     · 不叠 carry：|误差| 中位 0.30~0.84bp、均值偏差 -0.08~+0.21bp
    #     · 叠 carry  ：|误差| 中位 0.98~1.93bp、均值偏差 +0.98~+1.85bp（单向高估）
    #   即日票息（约 1bp）被算了两次。故 ①②③ 三条主口径一律**不叠 carry**；
    #   carry 只保留给 ④「无任何方向信号」的兜底（那时它是唯一的增量来源）。

    # ── ① 中证指数当日涨跌（收盘后可得，主口径） ──
    pct, pct_day = csi_pct_on(meta["idx"], target)
    if pct is not None and pct_day == target:
        est = latest_nav * (1.0 + pct / 100.0)
        out["est"] = round(est, 4)
        out["idx_chg"] = round(pct, 4)
        out["method"] = "csi_index_daily"
        lat = csi_latest(meta["idx"])
        out["idx_price"] = lat[1].get("close") if lat else None
        out["detail"] = {
            "估值状态": "中证指数日涨跌锚定（收盘后精确口径）",
            "锚点": {"日期": nav_date, "净值": latest_nav},
            "驱动": {"指数代码": meta["idx"], "指数名称": meta["idx_name"],
                     "指数涨跌%": round(pct, 4), "指数点位": out["idx_price"],
                     "指数交易日": pct_day,
                     "成份数": lat[1].get("cons") if lat else None},
            "票息": {"说明": "已含在指数涨幅内（财富指数口径），不重复叠加",
                     "日票息收益率%": round(carry * 100, 5) if carry else None,
                     "折元/份": round(latest_nav * carry, 6) if carry else None},
            "算法": {"id": ALGO_ID, "version": ALGO_VERSION,
                     "说明": "est = nav(T-1)×(1+指数涨跌%)；"
                             "指数为中证官网官方发布值，实测误差 |中位| <1bp。"
                             "中证债券指数为财富指数，涨幅已含票息，故不再加 carry"},
        }
        _log_state(code, True)
        return out

    # ── ② 盘中：无兄弟 ETF 时，用同族指数的「盘中最新日」做代理 ──
    #   仅 551030 走这里（它跟踪 932160，但市场无同指数 ETF）。
    #   932158 与 932160 日涨跌差 |中位| 0.00bp、max 1.00bp，代理回测 0.72bp。
    #   🔴 必须做日期对齐：代理指数的最新发布日若 <= 锚点净值日，说明那段行情
    #      **已经被官方净值消化**，再叠一次就是双重计算。实测不判这条，会把
    #      551030 的 11-16 指数 -0.12% 叠到 11-16 的净值上，凭空多跌 12bp。
    proxy = meta.get("idx_proxy")
    if proxy and not meta.get("peers_are_proxy"):
        lat = csi_latest(proxy)
        if lat:
            pday, pv = lat
            ppct = pv.get("pct")
            if ppct is not None and pday > nav_date:
                est = latest_nav * (1.0 + ppct / 100.0)
                out["est"] = round(est, 4)
                out["idx_chg"] = round(ppct, 4)
                out["idx_price"] = pv.get("close")
                out["method"] = "csi_index_proxy"
                out["note"] = ("本只跟踪指数 %s 当日值未发布，且市场无同指数兄弟 ETF；"
                               "改用同族指数 %s（%s）在 %s 的日涨跌 %.3f%% 代理"
                               "（实测两只指数日涨跌差 |中位| 0.00bp、max 1.00bp）"
                               % (meta["idx"], proxy, pv.get("name") or "", pday, ppct))
                out["detail"] = {
                    "估值状态": "同族指数代理（盘中/收盘后均可用）",
                    "锚点": {"日期": nav_date, "净值": latest_nav},
                    "驱动": {"本只指数": meta["idx"], "代理指数": proxy,
                             "代理指数名称": pv.get("name"),
                             "指数涨跌%": round(ppct, 4),
                             "指数点位": pv.get("close"),
                             "指数交易日": pday},
                    "票息": {"说明": "已含在代理指数涨幅内，不重复叠加",
                             "日票息收益率%": round(carry * 100, 5) if carry else None,
                             "折元/份": round(latest_nav * carry, 6) if carry else None},
                    "算法": {"id": ALGO_ID, "version": ALGO_VERSION,
                             "说明": "est = nav(T-1)×(1+代理指数涨跌%)；"
                                     "代理指数同为中证财富指数口径，涨幅已含票息"},
                }
                _log_state(code, True)
                return out

    # ── ③ 盘中：兄弟 ETF 反推（粗估） ──
    #   551030 走的也是这条，只是它的「兄弟」是**间接代理**（跟 932158 的四只），
    #   因为 932160 与 932158 日涨跌几乎完全同步（差 |中位| 0.00bp、max 1.00bp）。
    pr = peers_implied_nav(code)
    if pr:
        est = latest_nav * (1.0 + pr["pct"] / 100.0)
        out["est"] = round(est, 4)
        out["idx_chg"] = round(pr["pct"], 4)
        out["method"] = "peers_implied_intraday"
        _is_proxy = bool(meta.get("peers_are_proxy"))
        if pr.get("div_suspect"):
            out["note"] = (
                "疑似分红除权日：本只价格涨幅 %+.3f%% 与兄弟中位 %+.3f%% 相差 "
                "%.0fbp，改用本只自身价格涨幅（价格已除息，乘锚点即为除息后净值）"
                % (pr["own_pct"], pr["peers_pct"],
                   abs(pr["own_pct"] - pr["peers_pct"]) * 100))
        else:
            out["note"] = (
                "中证指数当日值尚未发布（盘中），用 %d 只%s ETF 的盘中涨跌中位数 "
                "%+.3f%% 推算，属**粗估**（假设今日折溢价与昨日相同，"
                "实测 P90 误差 25~36bp）；收盘后自动切为指数精确口径"
                % (pr["n"], "同族（间接代理）" if _is_proxy else "同指数",
                   pr["pct"]))
        out["detail"] = {
            "估值状态": ("分红除权日·改用本只价格涨幅"
                        if pr.get("div_suspect") else
                        "盘中粗估（%s涨幅反推）"
                        % ("同族ETF间接代理" if _is_proxy else "兄弟ETF")),
            "锚点": {"日期": nav_date, "净值": latest_nav},
            "驱动": {"口径": ("同族指数(%s)ETF 盘中涨跌幅中位数" % meta.get("idx_proxy")
                              if _is_proxy else "同指数兄弟ETF盘中涨跌幅中位数"),
                     "参与只数": pr["n"],
                     "兄弟涨幅中位%": round(pr["peers_pct"], 4),
                     "本只价格涨幅%": (round(pr["own_pct"], 4)
                                       if pr.get("own_pct") is not None else None),
                     "明细": pr["detail"][:6]},
            "票息": {"说明": "已含在 ETF 盘中涨幅内，不重复叠加",
                     "日票息收益率%": round(carry * 100, 5) if carry else None,
                     "折元/份": round(latest_nav * carry, 6) if carry else None},
            "注意": "该值含折溢价日间变动噪音，不可当精确净值；仅用于盘中方向参考",
            "算法": {"id": ALGO_ID, "version": ALGO_VERSION,
                     "说明": "est = 本只nav(T-1)×(1+兄弟涨幅中位%)；"
                             "ETF 二级价格涨幅本身含票息，故不再加 carry"},
        }
        if _is_proxy:
            out["detail"]["代理说明"] = (
                "本只跟踪 %s，市场无同指数 ETF；用跟踪 %s 的四只 ETF 代理，"
                "因两指数日涨跌差 |中位| 0.00bp、max 1.00bp"
                % (meta["idx"], meta.get("idx_proxy")))
        _log_state(code, True)
        return out

    # ── ④ 兜底：纯票息 ──
    if carry_amt > 0:
        out["est"] = round(latest_nav + carry_amt, 4)
        out["method"] = "carry_only"
        out["note"] = "指数与兄弟ETF均不可得，仅叠加票息（方向=0）"
        out["detail"] = {
            "估值状态": "纯票息（无方向信号）",
            "锚点": {"日期": nav_date, "净值": latest_nav},
            "票息": {"日票息收益率%": round(carry * 100, 5),
                     "折元/份": round(carry_amt, 6)},
            "算法": {"id": ALGO_ID, "version": ALGO_VERSION},
        }
        _log_state(code, False, "指数/兄弟ETF均不可得，退化为纯票息")
        return out

    # 全部不可得：宁空勿错（上层会显示最新净值）
    out["est"] = None
    out["method"] = "cash_bond_unavailable"
    out["note"] = ("中证指数、兄弟ETF、票息均不可得，无法估值；"
                   "宁留空也不拿最新净值冒充实时估值")
    out["detail"] = {"估值状态": "取数失败",
                     "锚点": {"日期": nav_date, "净值": latest_nav},
                     "算法": {"id": ALGO_ID, "version": ALGO_VERSION}}
    _log_state(code, False, "全部数据源不可得")
    return out


if __name__ == "__main__":
    import io
    import sys as _sys
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    for _c in CASH_BOND_ETF_CODES:
        _r = estimate_cash_bond_nav(_c)
        _m = CASH_BOND_ETF_META[_c]
        print("== %-7s %-16s est=%-10s nav=%-10s(%s) idx_chg=%-9s [%s]"
              % (_c, _m["name"], _r["est"], _r["latest_nav"], _r["nav_date"],
                 _r["idx_chg"], _r["method"]))
        if _r.get("note"):
            print("   note:", _r["note"][:140])
