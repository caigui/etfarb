# -*- coding: utf-8 -*-
"""历史核对页数据装配（2026-09-19 东哥要求 UI 完全对齐 LOF 套利程序）。

统一入口 `get_history(code, ...)`：
  · 现金管理 3 只（val_method=bond）→ bond_etf_valuation.static_history_bond
    （列：日期/净值/每日增加[前端算]/收盘价/折价几根毛/溢价/指数或期货/静态估值/误差）
    · 国内 LOF（lof_index）→ static_val.static_history + 富化：
    收盘价(新浪日K)、nav_chg、静态溢价=(price/static_val−1)、指数价/指数涨幅
    （index_history，缺源时 A 股指数新浪日K兜底回写，同 000012 先例）、
    份额/新增（unified_fund_history.shares/shares_added，VPS 万份口径）。

行统一降序；数据不足的字段留 None（前端显 '—'），宁空勿错。
`refresh=True` = 前端「核对静态估值（补采+重算）」按钮：清缓存强制回源。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import static_val as _sv                     # noqa: E402
import bond_etf_valuation as _bv             # noqa: E402
try:
    import saudi_bridge as _saudi            # 沙特双雄驱动（02830 × HKD/CNY）
except ImportError:
    from core import saudi_bridge as _saudi
from core.db import DB_PATH                   # noqa: E402

DATA_BEG = "2024-01-01"


def _driver_meta(code):
    """历史页「驱动」两列元信息 → (显示名, 单位, 小数位)。

    · 159687 → ('LCU', '净值', 4)：LCU 官方 NAV 桥接（前端显「LCU 净值 / LCU 涨幅」）
    · 520830/159329 → ('02830', '价', 2)：港股 02830 收盘价（「02830 价 / 02830 涨幅」）
    · 其余 → (None, None, 4)：无驱动列，回落到「指数价 / 指数涨幅」。
    """
    if code == '159687':
        return ('LCU', '净值', 4)
    if code in _saudi.FUNDS:
        return ('02830', '价', 2)
    return (None, None, 4)


def _profile(code, db):
    c = sqlite3.connect(db) if isinstance(db, str) else db
    own = c is not db
    try:
        r = c.execute(
            "SELECT fund_code, fund_name, jsl_index_nm, idx_code, val_method, fund_type, "
            "apply_fee, redeem_fee FROM fund_profile WHERE fund_code=?", (code,)).fetchone()
        if not r:
            return None
        keys = ["fund_code", "fund_name", "jsl_index_nm", "idx_code", "val_method",
                "fund_type", "apply_fee", "redeem_fee"]
        return dict(zip(keys, r))
    finally:
        if own:
            c.close()


def _nav_series(code, db):
    c = sqlite3.connect(db) if isinstance(db, str) else db
    own = c is not db
    try:
        return [(d, v) for d, v in c.execute(
            "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav>0 "
            "ORDER BY date", (code,))]
    finally:
        if own:
            c.close()


def _share_series(code, db):
    """{date: {shares, shares_added}}（unified_fund_history，VPS 万份口径）。"""
    c = sqlite3.connect(db) if isinstance(db, str) else db
    own = c is not db
    try:
        out = {}
        for d, s, n in c.execute(
                "SELECT date, shares, shares_added FROM unified_fund_history "
                "WHERE fund_code=? AND shares>0 ORDER BY date", (code,)):
            out[d] = {"shares": s, "shares_added": n}
        return out
    finally:
        if own:
            c.close()


def _sina_index_fallback(cs, db):
    """A 股指数新浪日K兜底（腾讯 fqkline 501 时用），回写 index_history。

    与 bond_etf_valuation._treasury_series 的 000012 先例同一哲学：
    消费侧只认 index_history 一张表，兜底写回同表并标 source='sina_kline'。"""
    sym = ("sz" if cs.startswith(("399", "159")) else "sh") + cs
    rows = _bv._fetch_sina_kline(sym)
    if len(rows) < 2:
        return
    c = sqlite3.connect(db) if isinstance(db, str) else db
    own = c is not db
    try:
        c.executemany(
            """INSERT OR REPLACE INTO index_history (symbol, date, close, source)
               VALUES (?, ?, ?, 'sina_kline')""",
            [(cs, d, v) for d, v in rows.items() if d >= DATA_BEG])
        c.commit()
    finally:
        if own:
            c.close()


def _index_series(idx_code, db, refresh=False):
    """跟踪指数 {date: close}（历史通道裸码口径；A 股指数缺源新浪兜底）。"""
    cs = _sv._hist_symbol(idx_code) if idx_code else None
    if not cs:
        return {}
    if refresh:
        try:
            _sv.fetch_index_history(idx_code, db)
        except Exception:
            pass
    else:
        try:
            _sv.ensure_index_history([idx_code], db)
        except Exception:
            pass
    c = sqlite3.connect(db) if isinstance(db, str) else db
    own = c is not db
    try:
        rows = {d: v for d, v in c.execute(
            "SELECT date, close FROM index_history WHERE symbol=? AND date>=? "
            "ORDER BY date", (cs, DATA_BEG))}
    finally:
        if own:
            c.close()
    if len(rows) < 2 and cs.startswith(("0", "3")):
        _sina_index_fallback(cs, db)
        c = sqlite3.connect(db) if isinstance(db, str) else db
        own = c is not db
        try:
            rows = {d: v for d, v in c.execute(
                "SELECT date, close FROM index_history WHERE symbol=? AND date>=? "
                "ORDER BY date", (cs, DATA_BEG))}
        finally:
            if own:
                c.close()
    return rows


def _close_on_or_before(series, date):
    best = None
    for d in series:
        if d <= date:
            best = d if (best is None or d > best) else best
        else:
            break
    return (best, series[best]) if best else (None, None)


def _history_lof(code, days, refresh, db):
    """LOF 历史行（降序）：static_val 引擎 + 价格/指数/份额富化。"""
    p = _profile(code, db) or {}
    idx_code = p.get("idx_code")
    # 净值补采：refresh=尽力回源；否则过期即补（均不致命，本地数据仍可读）
    try:
        if refresh:
            _sv.fetch_nav_history(code, db)
        else:
            _sv.ensure_nav_history([code], db)
    except Exception:
        pass
    # 🔴 先 ensure 指数序列（含 A 股指数新浪兜底回写），static_history 内部
    #   直接读 index_history —— 顺序反了会拿到空序列而整页 0 行。
    #   refresh 时 _index_series 内部已尽力回源（不致命），勿再重复抓取。
    idx_series = _index_series(idx_code, db, refresh) if idx_code else {}
    # 驱动序列（历史页「驱动 净值/价 + 涨幅」两列）：
    #   · 159687 → LCU 官方 NAV（LCU 桥接）
    #   · 520830/159329 → 港股 02830 收盘价（沙特桥接，估值驱动 = 02830 × HKD/CNY）
    drv_name, drv_unit, drv_dec = _driver_meta(code)
    drv_series = {}
    try:
        if code == '159687':
            lc = sqlite3.connect(db) if isinstance(db, str) else db
            own_lc = lc is not db
            lcu_rows = lc.execute(
                "SELECT date, nav FROM lcu_nav_history ORDER BY date").fetchall()
            if own_lc:
                lc.close()
            if len(lcu_rows) >= 2:
                drv_series = {d: v for d, v in lcu_rows}
        elif code in _saudi.FUNDS:
            drv_series = _saudi.hk_series(db)
    except Exception:
        drv_series = {}
    drv_dates = sorted(drv_series)
    base = _sv.static_history(code, days=days, db=db)   # 升序
    if not base:
        # 🔴 降级分支（2026-09-27）：指数历史缺失（无 idx_code / 无免费指数源，
        #   如 WATCH 池的美股指数 NDX/DJI/SP500、中证自定义 2.93xxxx、GPCSP006）
        #   ⇒ static_history 返回 []。但「净值 / 份额 / 新增」是真实数据，
        #   不应被指数缺失绑架整页空白。降级为「以净值序列为骨架」出行，
        #   指数与静态估值列留空（宁空勿错，行照常）。
        navs0 = _nav_series(code, db)
        base = [{"date": d, "nav": v, "static_val": None, "err_pct": None}
                for d, v in navs0]
    if not base:
        return []
    navs = _nav_series(code, db)
    nav_d = dict(navs)
    nav_arr = [d for d, _ in navs]
    # 价格（新浪日K）：失败不致命 → 价格/涨跌幅列留空，行照常（宁空勿错）
    try:
        px = _bv.fetch_fund_kline(code) or {}
    except Exception:
        px = {}
    px_dates = sorted(px)
    px_chg = {}
    for j, pd in enumerate(px_dates):
        prev_px = px[px_dates[j - 1]] if j > 0 and px[px_dates[j - 1]] else None
        px_chg[pd] = (px[pd], (px[pd] / prev_px - 1) * 100 if prev_px else None)
    shares = _share_series(code, db)

    idx_dates = sorted(idx_series) if idx_series else []

    out = []
    for r in base:
        d = r["date"]
        # nav_chg：全 nav 序列里的上一公布日
        i = nav_arr.index(d) if d in nav_arr else None
        nav_chg = None
        if i is not None and i > 0 and navs[i - 1][1]:
            nav_chg = (navs[i][1] / navs[i - 1][1] - 1) * 100
        price, price_chg = px_chg.get(d, (None, None))
        sv = r["static_val"]
        row = {
            "date": d, "nav": r["nav"], "nav_chg": nav_chg,
            "price": price, "price_chg": price_chg,
            "static_val": sv,
            "val_error": (sv - r["nav"]) if (sv is not None and r["nav"]) else None,
            "val_error_rate": ((sv - r["nav"]) / r["nav"] * 100)
                              if (sv is not None and r["nav"]) else None,
            "static_premium": (price / sv - 1) * 100 if (price and sv) else None,
        }
        if idx_series:
            ib, ib_px = _close_on_or_before(idx_series, d)
            if ib_px:
                prev_list = [x for x in idx_dates if x < ib]
                row["index_close"] = ib_px
                row["index_pct"] = (ib_px / idx_series[prev_list[-1]] - 1) * 100 \
                    if prev_list and idx_series[prev_list[-1]] else None
        # 驱动列：159687=LCU 净值 / 沙特双雄=02830 港股价（前端按 driver_name 显标题）
        if drv_series:
            ib, ib_px = _close_on_or_before(drv_series, d)
            if ib_px is not None:
                prev_list = [x for x in drv_dates if x < ib]
                _pct = (ib_px / drv_series[prev_list[-1]] - 1) * 100 \
                    if prev_list and drv_series[prev_list[-1]] else None
                row["drv_close"] = ib_px
                row["drv_pct"] = _pct
                # 向后兼容：ARM 上 index.html 若仍是旧版（只认 uses_lcu/lcu_*），
                # 159687 的 LCU 两列照常出数（新前端用 driver_name/drv_*）。
                if code == '159687':
                    row["lcu_close"] = ib_px
                    row["lcu_pct"] = _pct
        sh = shares.get(d)
        if sh:
            row["shares"] = sh["shares"]
            row["shares_added"] = sh["shares_added"]
        out.append(row)
    return list(reversed(out))[-days:]


# ── 2026-11-14 更新：历史记录页记录 511360 / 511520 更新后的估值算法 ──────────
def _cash_algo(code):
    """历史页头部下发的「当前估值算法」元信息（与实时入口同源）。"""
    try:
        # 2026-11-17 晚：9 只现金管理 ETF 的估值已接入（中证指数日涨跌锚定）。
        #   必须放在 511360/511520 之前判：这批代码不在 BOND_ETF_META 里，
        #   若掉到函数末尾的 bond_legacy「日均增长法」，会让人误以为它们
        #   用货币型的算法在算。
        if code in _CASH_BOND_ETF_CODES:
            import bond_cash_etf_valuation as _cbe
            return {"id": _cbe.ALGO_ID, "name": _cbe.ALGO_NAME,
                    "version": _cbe.ALGO_VERSION,
                    "entry": "core/bond_cash_etf_valuation.estimate_cash_bond_nav",
                    "desc": _cash_bond_desc(code)}
        # 2026-11-17 晚（请求 H）：可转债 ETF 两只。
        if code in _CB_ETF_CODES:
            import bond_cb_etf_valuation as _cbev
            return {"id": _cbev.ALGO_ID, "name": _cbev.ALGO_NAME,
                    "version": _cbev.ALGO_VERSION,
                    "entry": "core/bond_cb_etf_valuation.estimate_cb_etf_nav",
                    "desc": _cb_etf_desc(code)}
        # 历史遗留空壳（当前为空集）：保持原判据，防止未来又有人只入库不接算法。
        if code in _PENDING_VAL_CODES:
            return {"id": "pending", "name": "实时估值待接入",
                    "version": "—", "entry": "—",
                    "desc": ("该标的已收录到现金管理模块，实时/历史估值算法尚未接入。"
                             "接入方式：fund_profile.val_method 由 'pending' 改为 "
                             "'bond'，并在 core/bond_etf_valuation.BOND_ETF_META "
                             "登记算法入口。")}
        if code == '511360':
            import bond_511360_valuation as _b360
            return {"id": _b360.ALGO_ID, "name": _b360.ALGO_NAME,
                    "version": _b360.ALGO_VERSION, "entry": _b360.ALGO_ENTRY,
                    "desc": _b360.algorithm_desc()}
        if code == '511520':
            import bond_pcf_valuation as _bpv
            return {"id": _bpv.ALGO_ID, "name": _bpv.ALGO_NAME,
                    "version": _bpv.ALGO_VERSION,
                    "entry": "core/bond_pcf_valuation.estimate_511520_nav",
                    "desc": ("PCF 逐券篮子：替代金额=成份券全价。逐券价格优先级："
                             "① 当日有成交 → PCF全价+货币网真实成交净价跨日变动（零推算）；"
                             "② 无成交额 → 国开10Y活跃利率债Δy×该券修正久期；"
                             "③ 皆无 → CFETS逐券估值/曲线Δy。逐券反推票息计 carry；"
                             "债券99.52% + 一日逆回购0.48%(GC001)。盘后/周末价格效应"
                             "归零仍按票息+回购息计算，纯计算不冻结官方净值、不做校准拟合。")}
        if code in ('511090', '511130', '511220'):
            import bond_sse_pcf_valuation as _sse
            return {"id": _sse.ALGO_ID, "name": "上交所PCF逐券估值",
                    "version": _sse.ALGO_VERSION,
                    "entry": "core/bond_sse_pcf_valuation.estimate_sse_bond_nav",
                    "desc": ("上交所申赎清单公告只给数量不给替代金额 ⇒ 逐券价格由"
                             "货币网现券成交补齐：30Y特别国债按名称映射取Δy×组合久期，"
                             "成交稀疏时用 30Y国债期货TL 涨跌兜底；城投债ETF成份为"
                             "交易所券，用银行间城投/中票 Δy 中位数作同向代理 + 组合票息。"
                             "锚定最新官方净值只叠加增量，行情不新于净值日时显示锚点净值，"
                             "纯计算不拟合。回测 2026-11-13：511220 误差 +0.4bp。"
                             "历史页：30Y用TL日K代理方向，城投债按纯票息复算。")}
    except Exception:
        pass
    return {"id": "bond_legacy", "name": "日均增长法 / 票息+方向",
            "version": "—", "entry": "core/bond_etf_valuation.static_history_bond",
            "desc": "现金管理货币型沿用日均增长法。"}


def _cb_etf_desc(code):
    """可转债 ETF 的算法说明（历史页/弹窗共用）。"""
    if code == "511380":
        return ("驱动 = 中证可转债及可交换债券指数(931078) 的官方日涨跌 × "
                "滚动 beta(40 个交易日回归，当前≈0.99)。"
                "est = nav(T-1)×(1+beta×指数涨幅%)。"
                "**无票息项** —— 可转债利息已计入转债全价，不存在信用债那种 carry。"
                "实测样本外精度（54 个交易日）：|误差| 中位 1.2bp、RMSE 1.8bp、max 4.4bp，"
                "可当精确估值用。"
                "为何不走逐券：可转债在交易所确有连续成交（盘中成交额可达数亿元），"
                "但上交所 PCF 只给**数量**不给替代金额，qty×价格 无法还原真实权重"
                "（实测按数量加权算出的篮子涨跌比实际净值多跌 19bp），"
                "篮子恒等式 Σ(qty×px)/navpercu 在两只上分别是 10.414 / 10.781 —— 不是常数。"
                "故逐券只能算方向、算不准幅度，指数法反而准 20 倍。")
    return ("驱动 = 上证可转换债券指数(000139) 的官方日涨跌 × 滚动 beta"
            "(40 个交易日回归，当前≈0.84)。est = nav(T-1)×(1+beta×指数涨幅%)。"
            "⚠️ **本只精度有限，只能当参考**：实测样本外（54 个交易日）|误差| 中位 7.3bp、"
            "RMSE 11.0bp、max 42.4bp。两个根因：① 只持 85 只（511380 持 302 只），"
            "前几大是兴业/上银/重银等**银行转债**（低波动低 beta），个券集中度高；"
            "② beta 在漂移，4 个月内从 0.686 单调升到 0.817，滚动回归只能追不能预判。"
            "因此不要把它的估值当精确净值用，看方向与量级即可。"
            "（为何不走逐券：同 511380，PCF 缺替代金额，qty×价格 权重不可靠。）")


def _cash_desc(code):
    algo = _cash_algo(code)
    common = ("静态估值(D) = 公布 Nav(D) 之前的模型预估，随后与实际 Nav 核对；"
              "每行「估值算法」列记录该行实际使用的算法与数据来源，"
              "「估值误差/误差率」= 静态估值 − 实际净值。")
    if code == '511360':
        body = ("511360【已更新 {}，{}】：海富通官网 PCF 逐券篮子（方法二）"
                "——替代金额/(数量×10)=成份券全价，逐券 ΔP=−久期×Δy(成交bp中位数×0.9)"
                "+逐券票息；按三季报债券82.80%/买入返售17.16%(FR007)/其他0.04% 折算，"
                "锚定最新官方净值只取篮子相对变动。历史日按同方法 Δy=0 的纯票息口径复算；"
                "国债指数000012 仅作展示列，不参与估值。"
                .format(algo["id"], algo["version"]))
    elif code in _CASH_BOND_ETF_CODES:
        body = "【已更新 {}，{}】{}".format(algo["id"], algo["version"],
                                        _cash_bond_desc(code))
    elif code in _CB_ETF_CODES:
        body = "【已更新 {}，{}】{}".format(algo["id"], algo["version"],
                                        _cb_etf_desc(code))
    elif code in ('511090', '511130', '511220'):
        nm = {'511090': '511090 30年国债ETF鹏扬', '511130': '511130 30年国债ETF博时',
              '511220': '511220 城投债ETF海富通'}[code]
        body = ("{}【已更新 {}，{}】：上交所 PCF 公告（TradingDay=T 用 T-1 官方净值"
                "作锚点）+ 货币网现券成交补齐逐券价格。30Y 国债：成份券 Δy(活跃券)×"
                "组合修正久期，成交稀疏时用 TL 期货涨跌兜底；城投债：银行间城投/中票 "
                "Δy 中位数作同向代理 + 组合票息。行情不新于净值日时显示锚点净值，"
                "纯计算不拟合。历史页：30Y 用 TL 日K代理方向，城投债按纯票息复算。"
                .format(nm, algo["id"], algo["version"]))
    elif code == '511520':
        body = ("511520【已更新 {}，{}】：富国官网 PCF 逐券篮子为准"
                "——「替代金额」= 每只成份券全价（含应计）。逐券价格优先级："
                "① 当日有成交 → PCF 全价 + 货币网真实成交净价的跨日变动（零推算）；"
                "② 当日**无成交额** → 国开10Y活跃利率债 Δy × 该券修正久期（平行移动假设）；"
                "③ 两者皆无 → CFETS 逐券估值全价 / 曲线Δy。"
                "逐券反推票息计 carry；债券99.52% + 一日逆回购0.48%(GC001)。"
                "历史页按同一算法优先级复算：CFETS估值 > PCF逐日快照真实全价 > "
                "逐券carry+方向代理。纯计算，不冻结官方净值、不做校准拟合。"
                .format(algo["id"], algo["version"]))
    else:
        body = "511880/511990 货币型：近20日日均增量 × 计息天数（含周末）。"
    return body + "  " + common
_LOF_DESC = ("静态估值 = 前一日净值 × (1 + 0.95 × 指数涨幅)。"
             "它是基金公司公布净值**之前**的预估 —— 用它和随后公布的 Nav 核对答案，"
             "「误差」越小说明估值算法越准。")
_SAUDI_DESC = ("沙特双雄：静态估值 = 前一日净值 × 02830 港股涨跌 × 港币汇率涨跌。"
               "两只基金 100% 持有港股 02830（南方东英沙特阿拉伯ETF），"
               "故用其港股收盘价 × 港币中间价还原净值 —— 与官方口径同源，误差远小于纯指数法。")

# 🔴 2026-11-17 晚：9 只现金管理「交易所信用债 ETF」——估值已接入。
#   历史页与实时入口同源：驱动 = 中证指数官方日涨跌（财富指数，涨幅已含票息）。
_CASH_BOND_ETF_CODES = {
    "511070", "511110", "511190", "511200",      # 沪市 基准做市公司债（950245）
    "551900", "551500", "551550", "159600",      # 沪/深 科创债（932158）
    "551030",                                    # 沪市 AAA 科创债（932160）
}

# 历史遗留空壳（当前**为空集**）：仅保留兜底，防止未来又只入库不接算法。
#   2026-11-17 这批 9 只已从本集合移出、改由 _CASH_BOND_ETF_CODES 接管。
_PENDING_VAL_CODES = set()

# 🔴 2026-11-17 晚（请求 H）：可转债 ETF —— 估值已接入。
#   与上面 9 只信用债 ETF 的差异：可转债在交易所**有连续成交**，但 PCF 只给数量
#   不给替代金额（实测 qty×价格 推权重差 19~26bp，比指数法差 4~20 倍），
#   故仍用「转债指数日涨跌 × 滚动 beta」。
_CB_ETF_CODES = {"511380", "511180"}

# 「参考指数」列的展示名（只标注，不参与计算）
CASH_BOND_ETF_IDX_NM = {
    "511070": "上证基准做市公司债指数(950245)",
    "511110": "上证基准做市公司债指数(950245)",
    "511190": "上证基准做市公司债指数(950245)",
    "511200": "上证基准做市公司债指数(950245)",
    "551900": "中证科技创新公司债指数(932158)",
    "551500": "中证科技创新公司债指数(932158)",
    "551550": "中证科技创新公司债指数(932158)",
    "159600": "中证科技创新公司债指数(932158)",
    "551030": "中证AAA科技创新公司债指数(932160)",
}


def _cash_bond_desc(code):
    """这 9 只的算法说明（历史页/弹窗共用）。"""
    try:
        import bond_cash_etf_valuation as _cbe
        m = _cbe.CASH_BOND_ETF_META.get(code) or {}
    except Exception:
        m = {}
    idx, idxn = m.get("idx"), m.get("idx_name")
    proxy = m.get("idx_proxy")
    tail = ("本只跟踪的 %s 无同指数兄弟 ETF，收盘后用同族指数 %s 代理"
            "（两指数日涨跌差 |中位| 0.00bp、max 1.00bp）。" % (idx, proxy)
            if proxy else "")
    return (
        "驱动 = 中证指数官网 " + str(idx) + "（" + str(idxn) + "）的官方日涨跌。"
        "est = nav(T-1)×(1+指数涨幅)，**不叠加票息** —— 该指数是财富指数口径，"
        "涨幅本身已含当日票息，叠加会重复计息（实测 +1.0~1.9bp 单向高估）。"
        "盘中指数尚未发布时，用同指数兄弟 ETF 的盘中涨跌中位数粗估"
        "（P90 误差 25~36bp），收盘后自动切回指数精确口径。"
        "分红日按累计净值剔除派现后复算，不把除权当暴跌。"
        "实测精度（19 个交易日，vs 累计净值涨跌）：|误差| 中位 0.3~0.8bp、max 3.6bp。"
        "为何不走逐券：这批成份券是**交易所**信用债 —— 交易所行情恒为 0.000（无连续成交）、"
        "CFETS 估值表只覆盖政金债/国债（对 313 只成份券命中 0%）、"
        "货币网是银行间市场（名称命中 0 条且方向相反）。" + tail)


def get_history(code, days=60, refresh=False, db=None):
    """统一入口。返回 {code, name, kind: 'cash'|'lof', desc, profile, rows}。"""
    p = _profile(code, db) or {}
    if p.get("val_method") == "pending" or code in _PENDING_VAL_CODES:
        # 已收录但算法未接入：返回空 rows + 明确原因，不拿 LOF 的「净值×指数涨幅」
        # 或货币型的「日均增长」凑一个看起来像估值的数。
        algo = _cash_algo(code)
        return {"code": code, "name": p.get("fund_name") or "", "kind": "cash",
                "desc": algo["desc"], "algo": algo,
                "profile": {"index_nm": p.get("jsl_index_nm"),
                            "sub_fee": p.get("apply_fee"),
                            "redeem_fee": p.get("redeem_fee"),
                            "val_algo": algo["name"], "val_algo_id": algo["id"]},
                "rows": []}
    # 🔴 2026-11-17 晚：9 只现金管理交易所信用债 ETF 走这里。
    #   它们**不在** BOND_ETF_META 里（估值入口是独立模块），但历史口径同属
    #   「债券型静态估值」，由 static_history_bond 内部按 _CASH_BOND_ETF_CODES 分流。
    if (p.get("val_method") == "bond"
            and (code in _bv.BOND_ETF_META or code in _CASH_BOND_ETF_CODES
                 or code in _CB_ETF_CODES)):
        rows = _bv.static_history_bond(code, days=days, db=db, refresh=refresh)
        algo = _cash_algo(code)
        if code in _CASH_BOND_ETF_CODES:
            _idxnm = CASH_BOND_ETF_IDX_NM.get(code)
        elif code in _CB_ETF_CODES:
            _idxnm = _bv.CB_ETF_META_IDX.get(code)
        else:
            _idxnm = None
        return {"code": code, "name": p.get("fund_name") or "", "kind": "cash",
                "desc": _cash_desc(code), "algo": algo,
                "profile": {"index_nm": _idxnm,
                            "sub_fee": p.get("apply_fee"),
                            "redeem_fee": p.get("redeem_fee"),
                            "val_algo": algo["name"], "val_algo_id": algo["id"]},
                "rows": rows}
    rows = _history_lof(code, days, refresh, db)
    uses_lcu = any(r.get("drv_close") is not None for r in rows) and code == '159687'
    dn, du, dd = _driver_meta(code)
    desc = _SAUDI_DESC if code in _saudi.FUNDS else _LOF_DESC
    return {"code": code, "name": p.get("fund_name") or "", "kind": "lof",
            "desc": desc,
            "profile": {"index_nm": p.get("jsl_index_nm") or p.get("idx_name"),
                        "sub_fee": p.get("apply_fee"),
                        "redeem_fee": p.get("redeem_fee"),
                        "driver_name": dn, "driver_unit": du, "driver_dec": dd,
                        "uses_lcu": uses_lcu},
            "rows": rows}


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    dbp = DB_PATH
    for cc in ("511880", "511360", "511520", "161725"):
        h = get_history(cc, days=5, db=dbp)
        print("==", cc, h["kind"], "rows=%d" % len(h["rows"]))
        for r in h["rows"][:2]:
            print("  ", r)
