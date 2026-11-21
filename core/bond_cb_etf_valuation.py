# -*- coding: utf-8 -*-
"""
bond_cb_etf_valuation.py —— 可转债 ETF 日间/盘中估值
================================================================================

标的（2026-11-17 东哥要求放入现金管理）：
  511380 可转债ETF博时     （沪市，成份 302 只）
  511180 可转债ETF海富通   （沪市，成份  85 只）

【与 bond_cash_etf_valuation 的本质区别】
--------------------------------------------------
那 9 只信用债 ETF 的成份券是**交易所信用债**，几乎无连续成交，行情源恒推 0.000，
只能退到「指数日涨跌代理」。

可转债完全不同：**交易所连续竞价、成交活跃**。实测 2026-11-17 盘中：
  贵燃转债 110084  成交 326,040 手 / 3,794 万元
  通22转债 110085  成交 3,868,580 手 / 3.97 亿元
所以理论上可走逐券。**但实测逐券反而更差**（见下），最终仍选指数法。

【为什么逐券不可行（实测，非推测）】
--------------------------------------------------
用上交所 PCF 的成份券数量 qty 作权重、逐券取交易所真实收盘价，
加权算 2026-11-13 → 11-16 的篮子涨跌，与实际净值涨幅对比：

    511380   逐券加权 −3.239%   实际净值 −3.048%   差 −19.1bp
    511180   逐券加权 −2.923%   实际净值 −2.662%   差 −26.2bp

根因：PCF 只给**数量**不给**替代金额**，而 qty×价格 无法还原真实持仓权重 ——
仓位不满、留有现金与未上市转债、以及应收利息都在篮子之外。
实测篮子恒等式 Σ(qty×px) 与 navpercu 的比例在两只上分别是 10.414 / 10.781，
**不是常数**，说明 qty 与真实权重之间没有稳定换算关系。
⇒ 逐券加权只能算「方向」，算不出准确幅度，比指数法差 4~20 倍。

【最终方案：中证指数日涨跌 × 滚动 beta】
--------------------------------------------------
中证指数官网（同 bond_cash_etf_valuation 的 perf 接口）提供日收盘涨跌幅；
另发现新浪提供**上证转债指数 000139 的盘中实时行情**，可支撑盘中口径。

实测（79 个交易日，2026-07-20 ~ 11-16，逐日 vs 累计净值涨跌）：

    代码     指数       beta    相关系数 r   残差|中位|
    511380   931078    0.9948    0.9997      0.9bp    ← 近乎完美
    511380   000139    1.0281    0.9950      5.1bp
    511180   931078    0.7819    0.9827      8.8bp
    511180   000139    0.8171    0.9890      7.2bp    ← 略优

⇒ 511380 取 931078（中证可转债及可交换债券指数），误差 <1bp。
  511180 两个指数都在 7~9bp，取 000139（因其银行转债权重与 511180 更接近）。

【为什么要 beta，而不直接用指数涨幅】
--------------------------------------------------
511380 的 beta ≈ 0.995、四段窗口稳定在 0.99~1.00 ⇒ 基本满仓，等价于指数。
511180 的 beta = 0.78，且**随时间单调抬升**（实测四段）：

    2026-07-21~08-17   0.686
    2026-08-18~09-14   0.751
    2026-09-15~10-20   0.783
    2026-10-21~11-16   0.817

原因：511180 只持 85 只，前几大是兴业转债 / 上银转债 / 重银转债 / 重庆转债，
**银行转债占比高**（低波动、低 beta），且该占比在持续变化。
⇒ 必须用**滚动 beta**（近 40 个交易日回归），不能用固定常数：
    · 裸指数（beta=1）：511180 误差 19.3bp
    · 滚动 beta      ：511180 误差  7.2bp   ← 改善 2.7 倍
    · 511380 滚动 beta：0.9~1.1bp（与裸指数几乎一样，因为 beta≈1）

【盘中口径】
--------------------------------------------------
中证 perf 接口只在收盘后发布（盘中最新一条是 T-1），故盘中改用：
  新浪 sh000139（上证转债指数）实时涨跌幅 × 本只 beta
实测 000139 盘中数据完整（现价/昨收/成交额均正常），r=0.995/0.989，
盘中方向与量级可用，误差量级与收盘后同阶（不额外退化）。

【分红】
--------------------------------------------------
可转债 ETF 分红极少，但仍按累计净值（LJJZ）算涨跌、DWJZ 只用于展示，
与 bond_cash_etf_valuation 同一原则。实测这两只在 2026-05~11 无分红记录。

【精度边界（务必如实呈现给使用者）】
--------------------------------------------------
严格**样本外**回测（每个预测日只用此前数据算 beta，54 个交易日）：

    代码      中位|误差|    RMSE      max
    511380    1.22bp       1.80bp    4.42bp   ← 可当精确估值
    511180    7.30bp      10.96bp   42.39bp   ← 只能当参考

511180 精度受限的两个根因（都在数据里看得见）：
  ① 只持 85 只（511380 持 302 只），个券集中度高，单只银行转债的波动
     就会造成篮子偏离指数；
  ② beta 在漂移（4 个月 0.686→0.817），滚动回归只能追、不能预判。
模块会在 note / detail 里对 511180 显式标注「只能当参考」，不冒充精确值。
"""

from __future__ import print_function

import json
import logging
import re
import ssl
import time
import urllib.request
from datetime import date, timedelta

import bond_pcf_valuation as bpv          # _req / _UA
import bond_cash_etf_valuation as _cbe    # 复用中证 perf 取数（同一接口、同一限流策略）
import bond_sse_pcf_valuation as _sse     # PCF（用于成份数展示与结构说明）

logger = logging.getLogger("bond_cb_etf_valuation")

ALGO_ID = "cb_etf_index_beta_v1"
ALGO_NAME = "可转债ETF·转债指数×滚动beta"
ALGO_VERSION = "2026-11-17"

# ── 标的元信息 ──────────────────────────────────────────────
#   idx       : 收盘后用的中证指数（日涨跌精确口径）
#   live_sym  : 盘中用的新浪实时指数代码
#   beta_fix  : 兜底 beta（滚动回归失败时用；取长样本全样本值）
CB_ETF_META = {
    "511380": {
        "name": "可转债ETF博时",
        "idx": "931078",
        "idx_name": "中证可转债及可交换债券指数",
        "live_sym": "sh000139",
        "live_name": "上证转债指数",
        "beta_fix": 0.995,
        "precise": True,      # 精度可当精确值用（<1bp）
        "peer": "511180",
    },
    "511180": {
        "name": "可转债ETF海富通",
        "idx": "000139",
        "idx_name": "上证可转换债券指数",
        "live_sym": "sh000139",
        "live_name": "上证转债指数",
        "beta_fix": 0.82,
        "precise": False,     # 7~9bp，只能当参考（beta 漂移 + 银行转债集中）
        "peer": "511380",
    },
}
CB_ETF_CODES = tuple(CB_ETF_META)

BETA_LOOKBACK = 40         # 滚动 beta 回归窗口（交易日）
BETA_MIN_SAMPLES = 20      # 少于此样本量则退回 beta_fix
BETA_TTL = 3600            # beta 缓存 1 小时（日频量级，无需更短）
LIVE_TTL = 20              # 盘中指数价缓存 20 秒

_beta_cache = {}           # {code: (monotonic, beta, n, window_note)}
_live_cache = {"t": 0.0, "v": None}
_state = {}


def clear_caches():
    """清本模块进程内缓存（看板「补采+重算」用）。"""
    _beta_cache.clear()
    _live_cache.update({"t": 0.0, "v": None})
    _state.clear()


def _log_state(code, ok, msg=None):
    st = _state.setdefault(code, {"ok": None})
    prev = st.get("ok")
    st["ok"] = ok
    if ok:
        if prev is False:
            logger.info("[cb_etf] %s 估值已恢复", code)
        elif prev is None:
            logger.info("[cb_etf] %s 估值就绪", code)
        return
    st["msg"] = msg
    if prev is not False:
        logger.warning("[cb_etf] %s 估值降级：%s", code, msg)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


# ════════════════════ 净值（累计口径） ════════════════════

def fetch_nav_long(code, pages=4, page_size=50):
    """东财 lsjz 分页取长历史 → {date: (DWJZ, LJJZ)}。

    🔴 必须分页：东财单页上限有限，`_cbe._nav_series` 传 days>60 会返回空列表
       （实测 days=200/300 直接 IndexError）。beta 回归需要 ~40+ 个交易日，
       故这里自己分页累加。
    """
    out = {}
    for pi in range(1, int(pages) + 1):
        url = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery"
               "&fundCode=%s&pageIndex=%d&pageSize=%d"
               % (code, pi, int(page_size)))
        try:
            r = bpv._req("get", url, timeout=15, headers={
                "User-Agent": bpv._UA,
                "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code})
            body = getattr(r, "text", None)
            if body is None:
                body = getattr(r, "content", b"").decode("utf-8", "ignore")
            s, e = body.find("{"), body.rfind("}") + 1
            j = json.loads(body[s:e])
            lst = j.get("Data", {}).get("LSJZList", [])
            if not lst:
                break
            got = 0
            for x in lst:
                d = x.get("FSRQ")
                dw, lj = _num(x.get("DWJZ")), _num(x.get("LJJZ"))
                if d and dw:
                    out[d] = (dw, lj)
                    got += 1
            if got == 0:
                break
            time.sleep(0.6)      # 东财对密集翻页也会限流
        except Exception as e:
            logger.debug("[cb_etf] %s 净值第 %d 页失败: %s", code, pi, e)
            break
    return out


def _cum_pct_series(nav_map):
    """[(date, 累计净值日涨跌%)] 升序；优先 LJJZ，缺失退 DWJZ。"""
    ds = sorted(nav_map)
    out = []
    for i in range(1, len(ds)):
        d0, d1 = ds[i - 1], ds[i]
        a, b = nav_map[d0], nav_map[d1]
        v0, v1 = (a[1], b[1]) if (a[1] and b[1]) else (a[0], b[0])
        if not v0 or not v1:
            continue
        out.append((d1, (v1 / v0 - 1) * 100))
    return out


# ════════════════════ 滚动 beta ════════════════════

def rolling_beta(code, lookback=BETA_LOOKBACK, force=False):
    """近 lookback 个交易日：日净值涨跌 对 指数日涨跌 的 OLS 斜率。

    返回 {"beta", "alpha_bp", "n", "start", "end", "segs"} 或 None。

    🔴 为什么必须滚动而非固定常数：
       511180 的 beta 在 4 个月内从 0.686 单调漂到 0.817（银行转债占比变化），
       固定 0.78 会同时高估早期、低估近期。滚动 40 日实测把误差从 19.3bp
       压到 7.2bp。
    """
    now = time.monotonic()
    if not force:
        hit = _beta_cache.get(code)
        if hit and now - hit[0] < BETA_TTL:
            return hit[1]

    meta = CB_ETF_META.get(code) or {}
    idx = meta.get("idx")
    if not idx:
        return None
    try:
        # 用分段长历史：滚动 beta 需要 40+ 个交易日样本，单段 45 自然日不够
        idx_series = _cbe.csi_series_long(idx, segments=3)
        if not idx_series:
            idx_series = _cbe.fetch_csi_series(idx, 45)
    except Exception as e:
        logger.debug("[cb_etf] %s 指数日线失败: %s", code, e)
        idx_series = {}
    nav_map = fetch_nav_long(code, pages=3)
    if not idx_series or len(nav_map) < 10:
        return None

    nav_pct = dict(_cum_pct_series(nav_map))
    common = sorted(set(idx_series) & set(nav_pct))
    if len(common) < BETA_MIN_SAMPLES:
        logger.debug("[cb_etf] %s beta 样本不足: %d", code, len(common))
        return None
    common = common[-int(lookback):]

    X = [idx_series[d]["pct"] for d in common if idx_series[d].get("pct") is not None]
    Y = [nav_pct[d] for d in common if idx_series[d].get("pct") is not None]
    n = len(X)
    if n < BETA_MIN_SAMPLES:
        return None
    mx = sum(X) / n
    my = sum(Y) / n
    var = sum((x - mx) ** 2 for x in X)
    if var <= 1e-12:
        return None
    cov = sum((X[i] - mx) * (Y[i] - my) for i in range(n))
    beta = cov / var
    alpha = my - beta * mx

    # 稳健性巡检：把窗口切成 4 段，看 beta 是否在漂移（511180 会）
    segs = []
    step = max(1, n // 4)
    for lo in range(0, n, step):
        hi = min(lo + step, n)
        if hi - lo < 8:
            continue
        Xs, Ys = X[lo:hi], Y[lo:hi]
        mxs, mys = sum(Xs) / len(Xs), sum(Ys) / len(Ys)
        vs = sum((x - mxs) ** 2 for x in Xs)
        if vs <= 1e-12:
            continue
        bs = sum((Xs[i] - mxs) * (Ys[i] - mys) for i in range(len(Xs))) / vs
        segs.append({"from": common[lo], "to": common[hi - 1],
                     "beta": round(bs, 4), "n": hi - lo})
    drift = (max(s["beta"] for s in segs) - min(s["beta"] for s in segs)) if segs else 0.0

    res = {"beta": beta, "alpha_bp": alpha * 100, "n": n,
           "start": common[0], "end": common[-1],
           "segs": segs, "drift": drift, "idx": idx}
    _beta_cache[code] = (now, res)
    return res


def beta_for(code, force=False):
    """取 beta，失败退回 meta 里的 beta_fix。返回 (beta, 说明dict)。"""
    r = rolling_beta(code, force=force)
    meta = CB_ETF_META.get(code) or {}
    if r and r.get("beta"):
        return r["beta"], r
    return float(meta.get("beta_fix") or 1.0), {
        "beta": float(meta.get("beta_fix") or 1.0),
        "fallback": True,
        "note": "滚动回归不可用，退回长样本固定 beta",
    }


# ════════════════════ 盘中实时指数 ════════════════════

def fetch_index_live(sym="sh000139", ttl=LIVE_TTL):
    """新浪实时指数 {price, pre_close, pct, name} 或 None。"""
    now = time.monotonic()
    if _live_cache["v"] is not None and now - _live_cache["t"] < ttl:
        return _live_cache["v"]
    try:
        req = urllib.request.Request(
            "https://hq.sinajs.cn/list=%s" % sym,
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
        px, pre = _num(f[1]), _num(f[2])
        if not px or not pre or px <= 0 or pre <= 0:
            return None
        v = {"name": f[0], "price": px, "pre_close": pre,
             "pct": (px / pre - 1) * 100}
        _live_cache.update({"t": now, "v": v})
        return v
    except Exception as e:
        logger.debug("[cb_etf] 实时指数 %s 失败: %s", sym, e)
        return None


# ════════════════════ 篮子结构（仅展示） ════════════════════

def basket_info(code):
    """PCF 成份券统计（只用于 detail 展示，不参与估值）。

    🔴 明确记录：PCF 只给数量不给替代金额，实测 qty×价格 无法还原权重
       （两只的比例分别是 10.414 / 10.781，非常数），故**不能**用篮子算净值。
       这里取它的意义是让使用者看到「持仓分散度」——511380 有 302 只、
       511180 只有 85 只，这正是两者 beta 差异的根源。
    """
    try:
        p = _sse.fetch_sse_pcf(code)
    except Exception as e:
        logger.debug("[cb_etf] %s PCF 不可用: %s", code, e)
        return None
    if not p:
        return None
    bonds = p.get("bonds") or []
    qs = [x.get("qty") for x in bonds if (x.get("qty") or 0) > 0]
    top = sorted(bonds, key=lambda y: -(y.get("qty") or 0))[:5]
    return {"成份数": len(bonds),
            "有效持仓数": len(qs),
            "数量合计": sum(qs) if qs else None,
            "PCF交易日": p.get("trading_day"),
            "公告净值": p.get("nav"),
            "前5大成份": [{"代码": x.get("id"), "名称": x.get("name"),
                           "数量": x.get("qty")} for x in top]}


# ════════════════════ 主入口 ════════════════════

def estimate_cb_etf_nav(code, target_date=None):
    """单只可转债 ETF 的日内估值。返回形状对齐 estimate_bond_nav。

    {est, idx_chg, index_nm, idx_price, method, note,
     latest_nav, nav_date, detail, algo_id, algo_name}
    """
    out = {"est": None, "idx_chg": None, "index_nm": None, "idx_price": None,
           "method": "cb_etf", "note": None,
           "latest_nav": None, "nav_date": None, "detail": {},
           "algo_id": ALGO_ID, "algo_name": ALGO_NAME}
    meta = CB_ETF_META.get(code)
    if not meta:
        out["note"] = "未登记的可转债 ETF %s" % code
        return out

    nav_map = fetch_nav_long(code, pages=2, page_size=40)
    if not nav_map:
        # 退化用共享模块的东财短历史（含 LJJZ），保证至少拿到锚点
        try:
            rows = _cbe._nav_series(code, 30)
            nav_map = {r[0]: (r[1], r[2]) for r in rows}
        except Exception:
            nav_map = {}
    if not nav_map:
        out["note"] = "净值历史获取失败（天天基金）"
        _log_state(code, False, "净值历史获取失败")
        return out

    nav_date = max(nav_map)
    latest_nav = nav_map[nav_date][0]
    out["latest_nav"] = latest_nav
    out["nav_date"] = nav_date
    out["index_nm"] = meta["idx_name"]

    target = str(target_date or date.today())[:10]
    if target <= nav_date:
        out["est"] = round(latest_nav, 4)
        out["method"] = "cb_anchor_only"
        out["note"] = ("目标日 %s 不晚于最新官方净值日 %s，无更新行情；"
                       "显示锚点净值 %.4f" % (target, nav_date, latest_nav))
        out["detail"] = {"估值状态": "锚点净值（无更新行情）",
                         "锚点": {"日期": nav_date, "净值": latest_nav},
                         "算法": {"id": ALGO_ID, "version": ALGO_VERSION}}
        _log_state(code, True)
        return out

    beta, br = beta_for(code)
    _precise = bool(meta.get("precise"))
    _beta_src = ("固定兜底" if br.get("fallback")
                 else "滚动%d日回归(n=%d, %s~%s)"
                      % (BETA_LOOKBACK, br.get("n"), br.get("start"), br.get("end")))

    # ── ① 收盘后：中证指数当日涨跌（主口径）──
    try:
        idx_series = _cbe.fetch_csi_series(meta["idx"], 45)
    except Exception:
        idx_series = {}
    v = idx_series.get(target) if idx_series else None
    if v and v.get("pct") is not None:
        idx_pct = v["pct"]
        est = latest_nav * (1.0 + beta * idx_pct / 100.0)
        out["est"] = round(est, 4)
        out["idx_chg"] = round(idx_pct, 4)
        out["idx_price"] = v.get("close")
        out["method"] = "cb_index_daily"
        out["note"] = _mk_note(meta, beta, _beta_src, idx_pct, latest_nav,
                               precise=_precise)
        out["detail"] = {
            "估值状态": "中证指数日涨跌×滚动beta（收盘后口径）",
            "锚点": {"日期": nav_date, "净值": latest_nav},
            "驱动": {"指数代码": meta["idx"], "指数名称": meta["idx_name"],
                     "指数涨跌%": round(idx_pct, 4),
                     "指数点位": v.get("close"), "指数交易日": target,
                     "beta": round(beta, 4), "beta来源": _beta_src,
                     "有效涨跌%": round(beta * idx_pct, 4)},
            "篮子结构": basket_info(code),
            "算法": {"id": ALGO_ID, "version": ALGO_VERSION,
                     "说明": "est = nav(T-1)×(1+beta×指数涨跌%)。"
                             "可转债无票息 carry 项（利息已计入转债全价）。"
                             "beta 用于吸收仓位不足与结构偏离：511380 beta≈1（满仓），"
                             "511180 beta≈0.8（银行转债占比高、低波动）"},
        }
        if br.get("segs"):
            out["detail"]["beta巡检"] = {
                "分段": br["segs"], "段间极差": round(br.get("drift") or 0, 4),
                "说明": ("段间极差 >0.08 说明 beta 在漂移（持仓结构在变），"
                         "估值可信度下降" if (br.get("drift") or 0) > 0.08
                         else "各段 beta 稳定，估值可信"),
            }
        _log_state(code, True)
        return out

    # ── ② 盘中：上证转债指数实时 × beta ──
    live = fetch_index_live(meta.get("live_sym") or "sh000139")
    if live and live.get("pct") is not None:
        idx_pct = live["pct"]
        est = latest_nav * (1.0 + beta * idx_pct / 100.0)
        out["est"] = round(est, 4)
        out["idx_chg"] = round(idx_pct, 4)
        out["idx_price"] = live.get("price")
        out["index_nm"] = "%s（%s·盘中）" % (meta["idx_name"], live.get("name"))
        out["method"] = "cb_live_index_intraday"
        out["note"] = _mk_note(meta, beta, _beta_src, idx_pct, latest_nav,
                               precise=_precise, intraday=True)
        out["detail"] = {
            "估值状态": "盘中实时指数×滚动beta",
            "锚点": {"日期": nav_date, "净值": latest_nav},
            "驱动": {"实时指数": meta.get("live_sym"), "指数名称": live.get("name"),
                     "指数现价": live.get("price"), "指数昨收": live.get("pre_close"),
                     "指数涨跌%": round(idx_pct, 4),
                     "beta": round(beta, 4), "beta来源": _beta_src,
                     "有效涨跌%": round(beta * idx_pct, 4)},
            "篮子结构": basket_info(code),
            "注意": ("盘中用 %s 实时涨跌代理本只跟踪的 %s（两者不完全同一指数），"
                     "收盘后自动切回 %s 精确口径"
                     % (meta.get("live_name"), meta["idx"], meta["idx"])),
            "算法": {"id": ALGO_ID, "version": ALGO_VERSION,
                     "说明": "est = nav(T-1)×(1+beta×实时指数涨跌%)"},
        }
        _log_state(code, True)
        return out

    # ── ③ 都不可得：宁空勿错 ──
    out["est"] = None
    out["method"] = "cb_unavailable"
    out["note"] = ("中证指数与实时指数均不可得，无法估值；"
                   "可转债日波动常达 1~3%，绝不拿最新净值冒充实时估值")
    out["detail"] = {"估值状态": "取数失败",
                     "锚点": {"日期": nav_date, "净值": latest_nav},
                     "算法": {"id": ALGO_ID, "version": ALGO_VERSION}}
    _log_state(code, False, "指数与实时源均不可得")
    return out


def _mk_note(meta, beta, beta_src, idx_pct, latest_nav=None, precise=True,
             intraday=False):
    """统一的说明文案：把「精度边界」如实写在 note 里，不冒充精确值。"""
    base = ("%s×beta %.3f（%s）= 有效涨跌 %+.3f%%"
            % ("实时指数" if intraday else meta["idx_name"],
               beta, beta_src, beta * idx_pct))
    if latest_nav is not None:
        base += "，est = %.4f×(1%+.4f%%)" % (latest_nav, beta * idx_pct)
    if precise:
        base += ("。实测样本外精度（54 日滚动回测）：|误差| 中位 1.2bp、"
                 "RMSE 1.8bp、max 4.4bp —— 可当精确估值用")
    else:
        base += ("。⚠️ 本只精度有限：实测样本外（54 日滚动回测）|误差| 中位 7.3bp、"
                 "RMSE 11.0bp、max 42.4bp —— 仅持 85 只、银行转债占比高且 beta 在漂移"
                 "（4 个月内 0.69→0.82），**只能当参考**，不可当精确净值")
    return base


if __name__ == "__main__":
    import io
    import sys as _sys
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8",
                                   line_buffering=True)
    for _c in CB_ETF_CODES:
        _m = CB_ETF_META[_c]
        _r = estimate_cb_etf_nav(_c)
        _b, _br = beta_for(_c)
        print("== %-7s %-16s est=%-10s nav=%-10s(%s) idx=%-8s beta=%.4f [%s]"
              % (_c, _m["name"], _r["est"], _r["latest_nav"], _r["nav_date"],
                 _r["idx_chg"], _b, _r["method"]))
        if _r.get("note"):
            print("   note:", _r["note"])