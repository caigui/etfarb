# -*- coding: utf-8 -*-
"""债券 ETF 盘中估值（移植自 arbTest BondETFValuation，2026-09-18 东哥授权）。

仅服务于「现金管理」TAB 的 4 只：511880 / 511990 / 511360 / 511520。
算法口径与 arbTest/LOF套利程序完全一致（东哥金标准），逐项照搬：

  · 511880 / 511990 银华日利 / 华宝添益（货币型）：`日均增长法`
      货币基金净值稳增，取近 20 日日均增量（每日计息，含周末/节假日），
      从最新净值往前累加到今日。

  · 511360 海富通短融（短久期）：`独立方法 core/bond_511360_valuation.estimate_511360_nav`
      2026-11-14 起改用「方法二 PCF 逐券篮子」：海富通官网 PCF 替代金额=成份券全价，
      逐券 Δy×久期 + 票息，按三季报债券82.80%/买入返售17.16%(FR007)/其他0.04% 折算，
      锚定最新官方净值只取篮子相对变动。国债指数000012 仅作「指数涨幅」参考列，
      不参与估值。旧的「固定票息 0.003 + 0.07×国债指数」只在独立方法取数失败时兜底。
      算法集中在一个方法里，后续换算法只改 core/bond_511360_valuation.py。

  · 511520 富国政金（中久期）：`底仓票息 + 10Y国债期货方向`
      est = latest_nav + daily_coupon(0.02) + latest_nav × 活跃10Y国债期货涨跌幅%
      （合约按交割月动态取未到期最近月，2026-09-30 起不再硬编码 T2609；
       系数 1.0，与 arbTest estimate_today_nav 完全一致）

数据源（与 arbTest 对齐，但落地到 ETFarb 已验证通道）：
  · 净值历史：天天基金 f10/lsjz API（ETFarb 的 unified_fund_history(nav) 对这 3 只为空，
    必须现拉；arbTest 同款源）。进程内缓存 30 分钟。
  · 国债指数 000012：由调用方传入 `treasury_pct`（etf_iopv_quote 走
    index_quote.IndexQuoteProvider.get_change("000012")，即 s_sh000012，已实测通）。
  · 国债期货：本模块内新增新浪 CFF_RE_<活跃合约> 抓取（含 TF 辅助），
    活跃合约由 `_active_cffex_code` 动态计算（取代写死的 T2609）。
"""
import calendar
import json
import logging
import re
import ssl
import time
import threading
import urllib.request
from datetime import date, datetime, timedelta

logger = logging.getLogger("bond_etf_valuation")

# ── 511520 估值状态：只在「状态切换」时写日志（2026-11-13）──
#   看板 30 秒刷一次，若每次降级都 logger.warning，网络一断就是满屏重复告警，
#   真正有用的信息全被淹没。改为：ok → degraded/fallback/exception 记一次，
#   持续同一状态降为 debug，恢复时再记一次 info。
_511520_STATE = {"state": None}
_511360_STATE = {"degraded": False}     # 独立 511360 估值是否正常（仅状态切换告警）
_STATE_DESC = {"ok": "PCF 逐券正常",
               "degraded": "PCF 逐券已用降级数据（本地快照/部分推算）",
               "fallback": "已退化到旧口径（票息+期货方向）",
               "exception": "估值抛异常"}


def _log_511520_state(state, msg=None):
    prev = _511520_STATE["state"]
    if state == prev:
        if msg:
            logger.debug(msg)
        return
    _511520_STATE["state"] = state
    if msg is None:
        if state == "ok" and prev is not None:
            logger.info("[bond] 511520 估值已恢复正常（PCF 逐券）")
        return
    if state == "ok":
        logger.info(msg)
    elif prev in (None, "ok"):
        # 从正常掉下来：这是最该被看见的一次。
        # 但若 note 只是「网络不可达、用本地快照」这类**已在 bond_pcf_valuation
        # 里告警过同一根因**的情况，就不再重复 warning（同一次故障刷 3 条太吵）。
        _dup = any(t in (msg or "") for t in
                   ("网络暂不可达", "PCF 用本地快照", "用本地快照",
                    "尚未发布", "现券行情不可得", "已被官方净值",
                    "显示最新官方净值"))
        logger.info(msg) if _dup else logger.warning(msg)
    else:
        # 在异常状态间切换（degraded ↔ fallback），info 即可，别重复刷 warning
        logger.info(msg)


_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BOND_ETF_META = {
    '511880': {'name': '银华日利ETF', 'type': 'money_market', 'weekend_on': 'friday'},
    '511990': {'name': '华宝添益ETF', 'type': 'money_market', 'weekend_on': 'friday'},
    # 511360 的 daily_coupon / idx_coefficient 只是「独立 PCF 方法取数失败」时的
    # 旧口径兜底参数（2026-11-14），正常估值已不走这两个数。
    '511360': {'name': '短融ETF海富通', 'type': 'short_bond', 'weekend_on': 'monday',
               'daily_coupon': 0.003, 'idx_coefficient': 0.07,
               'algo': 'core/bond_511360_valuation.estimate_511360_nav'},
    # 🔴 511520 的 daily_coupon 只是「计算值取不到时」的兜底（2026-11-14）。
    #    原 0.02 元/份/日 ≈ 年化 6.1%，是真实组合票息(≈2.0%)的 3 倍，会系统性
    #    把静态估值抬高约 0.013 元/份/日。实际值由
    #    bond_pcf_valuation.static_carry_unit() 按成份券反推票息算得 ≈0.0064。
    '511520': {'name': '政金债ETF富国', 'type': 'mid_bond', 'weekend_on': 'monday',
               'daily_coupon': 0.0064},
    # 2026-11-14 新增：上交所债券 ETF（PCF 公告无替代金额，逐券价格由
    # 货币网现券/TL期货补齐；算法在 core/bond_sse_pcf_valuation.py）。
    '511090': {'name': '30年国债ETF鹏扬', 'type': 'sse_bond', 'weekend_on': None,
               'algo': 'core/bond_sse_pcf_valuation.estimate_sse_bond_nav'},
    '511130': {'name': '30年国债ETF博时', 'type': 'sse_bond', 'weekend_on': None,
               'algo': 'core/bond_sse_pcf_valuation.estimate_sse_bond_nav'},
    '511220': {'name': '城投债ETF海富通', 'type': 'sse_bond', 'weekend_on': None,
               'algo': 'core/bond_sse_pcf_valuation.estimate_sse_bond_nav'},
}

# 历史页新三只的回测参数（与 core/bond_sse_pcf_valuation 实时口径同源；
# TL_RATIO 变动时两处同步 —— 这里独立定义避免循环导入）。
_SSE_TL_RATIO = 0.97     # 30Y 组合久期 / TL 久期 的经验比值
_SSE_Y30 = 2.07          # 30Y 特别国债组合收益率近似（%年化）
_SSE_Y_CREDIT = 1.90     # 城投组合收益率近似（%年化）

# 🔴 2026-11-17：先收录、估值算法后续补的标的。
#   2026-11-17 晚已接入算法（见 core/bond_cash_etf_valuation.py），本集合现仅作
#   「历史遗留空壳」兜底用；正常流程这些代码都会先命中 _CASH_BOND_ETF_CODES 分支。
_PENDING_VAL_CODES = {
    "511070", "511110", "511190", "511200",      # 沪市 基准做市公司债
    "551900", "551030", "551500", "551550",      # 沪市 AAA 科创债
    "159600",                                    # 深市 AAA 科创债
}

# 🔴 2026-11-17：现金管理类「交易所信用债 ETF」——估值已接入。
#   驱动 = 中证指数官方日涨跌（收盘后精确），盘中用同类 ETF 反推粗估。
#   为什么不走逐券：这批成份券是交易所信用债，交易所行情恒为 0.000（无连续成交），
#   CFETS 估值表只覆盖政金债/国债（对 313 只成份券命中 0%），货币网是银行间市场
#   （名称命中 0 条，且 MTN 的 Δy 与之方向相反）。详见 bond_cash_etf_valuation
#   模块 docstring 的实测证据表。
_CASH_BOND_ETF_CODES = {
    "511070", "511110", "511190", "511200",
    "551900", "551030", "551500", "551550", "159600",
}

# 历史页「参考指数」列：只做展示标注，不参与计算。
# 与 bond_cash_etf_valuation.CASH_BOND_ETF_META 保持一致（那里才是算法唯一来源）。
CASH_BOND_ETF_META_IDX = {
    "511070": "950245", "511110": "950245", "511190": "950245",
    "511200": "950245", "551900": "932158", "551500": "932158",
    "551550": "932158", "159600": "932158", "551030": "932160",
}
CASH_BOND_ETF_META_NM = {
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

# 🔴 2026-11-17 晚（请求 H）：可转债 ETF —— 东哥要求放进现金管理。
#   与上面 9 只信用债 ETF 的关键差异：可转债在交易所**有连续成交**，但 PCF 只给
#   数量不给替代金额（实测 qty×价格 推权重差 19~26bp），不如指数法。
#   驱动 = 转债指数日涨跌 × 滚动 beta（511380 取 931078、511180 取 000139）。
#   详见 core/bond_cb_etf_valuation.py 的样本外回测。
_CB_ETF_CODES = {"511380", "511180"}
# 历史回测用的滚动 beta 参数（与 bond_cb_etf_valuation 保持一致）
_CB_BETA_LOOKBACK = 40
_CB_BETA_MIN = 20
_CB_ETF_BETA_FIX = {"511380": 0.995, "511180": 0.82}
# 展示列：跟踪的转债指数
CB_ETF_META_IDX = {
    "511380": "中证可转债及可交换债券指数(931078)",
    "511180": "上证可转换债券指数(000139)",
}

NAV_CACHE_TTL = 1800      # 净值历史缓存 30 分钟
_IDX_CACHE_TTL = 30       # 期货行情缓存 30 秒


# ── 中金所国债期货：活跃合约动态计算（取代硬编码 T2609，2026-09-30）──
def _cffex_delivery_months():
    return (3, 6, 9, 12)


def _second_friday(y, m):
    """CFFEX 国债期货最后交易日 = 交割月第 2 个周五（用于判断是否已到期）。"""
    first = date(y, m, 1)
    off = (4 - first.weekday()) % 7
    return date(y, m, 1 + off + 7)


def _active_cffex_code(prefix="T", as_of=None):
    """取未到期的最近交割月合约代码（如 2026-09 → T2612）。

    as_of 用于历史弹窗：按估值日取当时活跃合约，实现自动换月。"""
    as_of = as_of or date.today()
    cands = []
    for y in (as_of.year, as_of.year + 1):
        for m in _cffex_delivery_months():
            if y == as_of.year and m < as_of.month:
                continue
            if _second_friday(y, m) >= as_of:
                cands.append("%s%02d%02d" % (prefix, y % 100, m))
    return cands[0] if cands else "%s%02d12" % (prefix, as_of.year % 100)


# ── 交易日历（国庆调休等；⚠️ 2026 具体安排需东哥核对官方通知）──
_CN_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25",                     # 中秋
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",   # 国庆 10-1~7（用户明示）
}
_CN_MAKEUP_2026 = {                # 调休上班的周末（视为交易日）
    "2026-09-27", "2026-10-10", "2026-10-11",
}


def _is_trading_day(d):
    s = d.strftime("%Y-%m-%d")
    if s in _CN_MAKEUP_2026:
        return True
    if d.weekday() >= 5:
        return False
    return s not in _CN_HOLIDAYS_2026



# ── 净值历史：天天基金 f10/lsjz ──
def _fetch_nav_history(code, days=30):
    url = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
           "&pageIndex=1&pageSize=%d" % (code, days))
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code,
    })
    raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
    s = raw.find("{"); e = raw.rfind("}") + 1
    if s < 0 or e <= s:
        return []
    data = json.loads(raw[s:e])
    recs = data.get("Data", {}).get("LSJZList", [])
    out = []
    for r in recs:
        d = r.get("FSRQ"); nav = r.get("DWJZ")
        if d and nav:
            try:
                out.append((d, float(nav)))
            except (ValueError, TypeError):
                continue
    return out


def _latest_nav_qq_jj(code):
    """腾讯 jj 实时单位净值（字段[3]）。用于 eastmoney lsjz 尺度异常的基金
    （如 511990：东财返 0.275 而非 ~100；腾讯 year_all 为空）。返回 float 或 None。"""
    try:
        raw = urllib.request.urlopen(urllib.request.Request(
            "https://qt.gtimg.cn/q=sh%s" % code,
            headers={"User-Agent": _UA, "Referer": "https://gu.qq.com/"}),
            timeout=8, context=_CTX).read().decode("gbk", "ignore")
        m = re.search(r'v_sh%s="([^"]+)"' % code, raw)
        if not m:
            return None
        return float(m.group(1).split("~")[3])
    except Exception:
        return None


class _BondNavCache:
    """进程内净值历史缓存（按 code_days）。"""
    def __init__(self):
        self._nav = {}
        self._lock = threading.Lock()

    def get_history(self, code, days=30):
        key = "%s_%d" % (code, days)
        with self._lock:
            cached = self._nav.get(key)
            if cached and time.monotonic() - cached[0] < NAV_CACHE_TTL:
                return cached[1]
        # 🔴 必须兜住异常（2026-11-13）：_fetch_nav_history 是裸 urllib，
        #    网络不通时 urlopen 直接抛 URLError，会把整个 511520 估值分支打断、
        #    看板 500。这里吞掉并返回 []，让上层走"官方净值获取失败"的降级路径。
        try:
            recs = _fetch_nav_history(code, days)
            _nav_state["failed"] = False
        except Exception as _e:
            if not _nav_state["failed"]:
                logger.warning("[bond] 净值历史获取失败（将自动重试）: %s: %s",
                               type(_e).__name__, _e)
                _nav_state["failed"] = True
            else:
                logger.debug("[bond] 净值历史仍不可用: %s: %s", type(_e).__name__, _e)
            recs = []
        if recs:
            if _nav_state["failed"]:
                logger.info("[bond] 净值历史已恢复（%s，%d 条）", code, len(recs))
                _nav_state["failed"] = False
            with self._lock:
                self._nav[key] = (time.monotonic(), recs)
        return recs


_nav_state = {"failed": False}       # 净值历史取数状态（仅状态切换时告警）
_nav_cache = _BondNavCache()


def calc_avg_daily_growth(history, weekend_on, days=20):
    """history: [(date_str, nav), ...] 降序（[0] 最新）。返回日均增量或 None。"""
    if len(history) < 3:
        return None
    daily_rates = []
    for i in range(len(history) - 1, 0, -1):
        older = history[i]
        newer = history[i - 1]
        nav_change = newer[1] - older[1]
        try:
            d1 = datetime.strptime(older[0], "%Y-%m-%d")
            d2 = datetime.strptime(newer[0], "%Y-%m-%d")
            delta_days = (d2 - d1).days
        except ValueError:
            delta_days = 1
        # 货币型每日计息（含周末/节假日），accrual = 自然日天数
        accrual = max(delta_days, 1)
        daily_rates.append(nav_change / accrual)
    if not daily_rates:
        return None
    recent = daily_rates[-min(len(daily_rates), days):]
    if len(recent) >= 4:
        mean = sum(recent) / len(recent)
        std = (sum((x - mean) ** 2 for x in recent) / len(recent)) ** 0.5
        filtered = [x for x in recent if abs(x - mean) < 5 * std]
        if len(filtered) >= 3:
            recent = filtered
    return sum(recent) / len(recent)


# ── 国债期货 T2609 / TF2609（新浪）──
_futures_cache = {"t": 0.0, "v": None}
_futures_lock = threading.Lock()


def get_treasury_futures_quote():
    """返回 (t_pct, tf_pct, t_price) 或 (None, None, None)。进程内缓存 30s。

    t_price = 活跃 10Y 国债期货最新价（「指数价」列展示用）。
    合约按交割月动态取未到期最近月（2026-09-30 起不再硬编码 T2609）。"""
    now = time.monotonic()
    with _futures_lock:
        if _futures_cache["v"] is not None and now - _futures_cache["t"] < _IDX_CACHE_TTL:
            v = _futures_cache["v"]
            return v[0], v[1], v[2] if len(v) > 2 else None
    t_pct = tf_pct = t_price = None
    try:
        t_code = _active_cffex_code("T")
        tf_code = _active_cffex_code("TF")
        url = "https://hq.sinajs.cn/list=CFF_RE_%s,CFF_RE_%s" % (t_code, tf_code)
        req = urllib.request.Request(url, headers={
            "Referer": "https://finance.sina.com.cn/", "User-Agent": _UA})
        raw = urllib.request.urlopen(req, timeout=8, context=_CTX).read().decode("gbk", "ignore")
        for line in raw.strip().splitlines():
            if '="' not in line:
                continue
            fields = line.split('"')[1].split(",")
            if len(fields) < 10:
                continue
            try:
                latest = float(fields[0])
                yest_settle = float(fields[1])
            except ValueError:
                continue
            if yest_settle <= 0:
                continue
            pct = (latest - yest_settle) / yest_settle * 100
            if 'CFF_RE_%s' % t_code in line:
                t_pct = round(pct, 4)
                t_price = latest
            elif 'CFF_RE_%s' % tf_code in line:
                tf_pct = round(pct, 4)
    except Exception:
        pass
    res = (t_pct, tf_pct, t_price)
    with _futures_lock:
        _futures_cache["t"] = now
        _futures_cache["v"] = res
    return res


def get_treasury_futures_pct():
    """兼容旧调用：返回 (t_pct, tf_pct) 或 (None, None)。"""
    t, tf, _ = get_treasury_futures_quote()
    return t, tf


# ── 对外：单只估值 ──
def estimate_bond_nav(code, treasury_pct=None, futures_pct=None):
    """返回 dict：{est, idx_chg, index_nm, method, note, latest_nav, nav_date}。

    est      日内预估净值（None=取数失败，调用方退化为最新净值）
    idx_chg  展示用「指数涨幅」：511360=国债指数%、511520=活跃10Y国债期货%、511880=None
    index_nm 对应指数名（前端展示）；511880=None
    """
    meta = BOND_ETF_META.get(code)
    out = {"est": None, "idx_chg": None, "index_nm": None,
           "method": "unknown", "note": None, "latest_nav": None, "nav_date": None}

    # ══ 现金管理类交易所信用债 ETF（2026-11-17 接入）══
    #   必须放在 `if not meta` 之前：这 9 只**故意不登记**在 BOND_ETF_META 里，
    #   因为它们的估值入口完全独立（中证指数驱动，不走国债期货/货币型日均增长）。
    #   驱动链路与实测证据见 core/bond_cash_etf_valuation.py。
    if code in _CASH_BOND_ETF_CODES:
        try:
            import bond_cash_etf_valuation as _cbe
            _r = _cbe.estimate_cash_bond_nav(code)
        except Exception as _e:
            logger.warning("[bond] %s 现金债 ETF 估值异常: %s", code, _e)
            _r = None
        if _r and _r.get("est") is not None:
            out["est"] = _r["est"]
            out["method"] = _r.get("method") or "cash_bond"
            out["note"] = _r.get("note")
            out["latest_nav"] = _r.get("latest_nav")
            out["nav_date"] = _r.get("nav_date")
            out["idx_chg"] = _r.get("idx_chg")
            out["index_nm"] = _r.get("index_nm")
            out["idx_price"] = _r.get("idx_price")
            out["pcf_detail"] = _r.get("detail")
            out["algo_id"] = _r.get("algo_id")
            out["algo_name"] = _r.get("algo_name")
            return out
        # 🔴 取数失败时**不能**回退成最新净值：那会把昨日净值冒充今日实时估值，
        #   溢价算成 0，比留空更误导（2026-11-17 请求 F 的教训）。
        out["est"] = None
        out["method"] = "cash_bond_unavailable"
        out["note"] = (_r or {}).get("note") or (
            "%s 现金债 ETF 估值取数失败；留空而不拿最新净值冒充实时估值" % code)
        out["latest_nav"] = (_r or {}).get("latest_nav")
        out["nav_date"] = (_r or {}).get("nav_date")
        # algo_id 即使取数失败也必须带上：etf_iopv_quote 靠它识别
        # 「这是现金债 ETF，不允许把最新净值兜底成实时估值」。
        out["algo_id"] = ((_r or {}).get("algo_id")
                          or "cash_bond_etf_csi_index_v1")
        out["algo_name"] = (_r or {}).get("algo_name")
        return out

    # ══ 可转债 ETF（2026-11-17 晚，请求 H）══
    #   同样必须在 `if not meta` 之前：这两只也不登记在 BOND_ETF_META。
    #   算法 = 转债指数日涨跌 × 滚动 beta，入口在 bond_cb_etf_valuation。
    if code in _CB_ETF_CODES:
        try:
            import bond_cb_etf_valuation as _cbev
            _r = _cbev.estimate_cb_etf_nav(code)
        except Exception as _e:
            logger.warning("[bond] %s 可转债 ETF 估值异常: %s", code, _e)
            _r = None
        if _r and _r.get("est") is not None:
            out["est"] = _r["est"]
            out["method"] = _r.get("method") or "cb_etf"
            out["note"] = _r.get("note")
            out["latest_nav"] = _r.get("latest_nav")
            out["nav_date"] = _r.get("nav_date")
            out["idx_chg"] = _r.get("idx_chg")
            out["index_nm"] = _r.get("index_nm")
            out["idx_price"] = _r.get("idx_price")
            out["pcf_detail"] = _r.get("detail")
            out["algo_id"] = _r.get("algo_id")
            out["algo_name"] = _r.get("algo_name")
            return out
        # 取数失败：留空，绝不拿最新净值冒充（可转债日波动 1~3%，冒充后果严重）
        out["est"] = None
        out["method"] = "cb_unavailable"
        out["note"] = (_r or {}).get("note") or (
            "%s 可转债 ETF 估值取数失败；留空而不拿最新净值冒充实时估值" % code)
        out["latest_nav"] = (_r or {}).get("latest_nav")
        out["nav_date"] = (_r or {}).get("nav_date")
        out["algo_id"] = ((_r or {}).get("algo_id") or "cb_etf_index_beta_v1")
        out["algo_name"] = (_r or {}).get("algo_name")
        return out

    if not meta:
        # 已收录但算法确实未接入的历史遗留标的（当前为空集，保留兜底）。
        if code in _PENDING_VAL_CODES:
            out["method"] = "pending"
            out["note"] = "实时估值待接入（已收录，估值算法后续补充）"
        else:
            out["note"] = "未知债券基金 %s" % code
        return out

    history = _nav_cache.get_history(code, days=30)
    if not history:
        out["note"] = "净值历史获取失败（天天基金）"
        return out
    latest_date, latest_nav = history[0]
    out["latest_nav"] = latest_nav
    out["nav_date"] = latest_date
    today = datetime.now().strftime("%Y-%m-%d")
    # 511990 等 eastmoney lsjz 返错误尺度（0.275 而非 ~100）；腾讯 year_all 为空。
    # 改用腾讯 jj 实时净值（仅当前值，无历史序列）。
    # ⚠️ 511220 例外：净值真实就在 ~10.44（1 元面值发行），不是尺度异常。
    if (latest_nav is None or latest_nav < 50) and code != '511220':
        jj_nav = _latest_nav_qq_jj(code)
        if jj_nav:
            out["latest_nav"] = jj_nav
            out["nav_date"] = today
            out["est"] = round(jj_nav, 4)
            out["method"] = "jj_realtime_no_history"
            out["note"] = ("%s eastmoney净值尺度异常，改用腾讯jj实时净值"
                           "（无历史序列，仅展示当前值）" % code)
            out["idx_chg"] = None
            out["index_nm"] = None
            return out

    # 🔴 源心跳：净值日期滞后 >1 个交易日（跳过周末/节假日），提示源更新中断。
    if out["nav_date"]:
        try:
            _nd = datetime.strptime(out["nav_date"], "%Y-%m-%d").date()
            _gap = 0
            _cur = _nd
            while _cur < date.today():
                _cur += timedelta(days=1)
                if _is_trading_day(_cur):
                    _gap += 1
            if _gap > 1:
                logger.warning("[bond] %s 净值日期滞后 %d 个交易日（nav_date=%s），疑似源更新中断",
                               code, _gap, out["nav_date"])
        except ValueError:
            pass

    # ══ 511360：独立方法（方法二 PCF 逐券篮子，2026-11-14 起）══
    #   换算法只改 core/bond_511360_valuation.estimate_511360_nav；这里只负责
    #   调用 + 失败降级。国债指数 000012 只作「指数涨幅」参考展示，不参与估值。
    if code == '511360':
        try:
            import bond_511360_valuation as _b360
            _r = _b360.estimate_511360_nav(treasury_pct=treasury_pct)
        except Exception as _e:
            _r = None
            logger.debug("[bond] 511360 独立估值异常: %s", _e)
        if _r and _r.get("est") is not None:
            if _511360_STATE["degraded"]:
                logger.info("[bond] 511360 PCF 逐券估值已恢复")
                _511360_STATE["degraded"] = False
            out["est"] = _r["est"]
            out["method"] = _r.get("method") or "pcf_basket_511360"
            out["note"] = _r.get("note")
            out["latest_nav"] = _r.get("latest_nav")
            out["nav_date"] = _r.get("nav_date")
            out["idx_chg"] = treasury_pct
            out["index_nm"] = "国债指数"
            out["pcf_detail"] = _r.get("detail")
            out["algo_id"] = _r.get("algo_id")
            out["algo_name"] = _r.get("algo_name")
            return out
        # 降级：旧口径（固定票息 + 国债指数线性），method 明确标 legacy_
        if not _511360_STATE["degraded"]:
            logger.warning("[bond] 511360 PCF 逐券估值不可用，临时退化为旧口径"
                           "（票息+国债指数×0.07）")
            _511360_STATE["degraded"] = True
        daily_coupon = meta.get('daily_coupon', 0.003)
        idx_coeff = meta.get('idx_coefficient', 0.07)
        _cp = daily_coupon if _is_trading_day(date.today()) else 0.0
        if treasury_pct is None:
            est = latest_nav + _cp
        else:
            est = latest_nav + _cp + round(treasury_pct * idx_coeff, 4)
        out["est"] = round(est, 4)
        out["method"] = "legacy_coupon_treasury_linear"
        out["note"] = ("511360 PCF 逐券估值取数失败，临时用旧口径"
                       "（nav + 0.003 票息 + 0.07×国债指数涨幅）")
        out["idx_chg"] = treasury_pct
        out["index_nm"] = "国债指数"
        return out

    # ══ 511090 / 511130 / 511220：上交所债券 ETF PCF 逐券估值（2026-11-14 起）══
    #   上交所公告只给「数量」不给替代金额 ⇒ 逐券价格由货币网现券成交
    #   （30Y 特别国债名称映射）或 30Y 国债期货 TL 兜底；511220 的交易所
    #   城投债用银行间城投/中票 Δy 中位数作同向代理。算法可整体替换：
    #   只改 core/bond_sse_pcf_valuation.estimate_sse_bond_nav。
    if code in ('511090', '511130', '511220'):
        try:
            import bond_sse_pcf_valuation as _sse
            _r = _sse.estimate_sse_bond_nav(code)
        except Exception as _e:
            logger.warning("[bond] %s 上交所 PCF 估值异常: %s", code, _e)
            _r = None
        if _r and _r.get("est") is not None:
            out["est"] = _r["est"]
            out["method"] = _r.get("method") or "sse_pcf"
            out["note"] = _r.get("note")
            out["latest_nav"] = _r.get("latest_nav")
            out["nav_date"] = _r.get("nav_date")
            out["idx_chg"] = _r.get("idx_chg")
            out["index_nm"] = _r.get("index_nm")
            out["idx_price"] = _r.get("idx_price")
            out["pcf_detail"] = _r.get("detail")
            out["algo_id"] = "bond_sse_pcf_v1"
            return out
        # 降级：锚点净值直显（宁显净值不乱估），method 明确标 degraded_
        out["est"] = round(latest_nav, 4)
        out["method"] = "degraded_sse_pcf_nav_only"
        out["note"] = ("%s 上交所 PCF 估值取数失败，显示最新官方净值" % code)
        return out

    # ══ 511520：PCF 逐券实时估值（2026-11-13 起，取代「票息+10Y国债期货方向」）══
    # 🔴 口径：富国官网 PCF 替代金额 = 每只成份券的实际全价（含应计利息），
    #   盘中逐券叠加「本券收益率变动 × 久期」+ 每日应计利息，再按篮子公式汇总：
    #       NAV_est = (Σ 数量×10×px_est + 预估现金) ÷ 最小申赎单位
    #   篮子恒等式校验可精确复现官方净值（实测偏差 <0.01bp）。
    #   详细实现见 core/bond_pcf_valuation.estimate_511520_nav（方法二原型移植）。
    #   「指数价 / 指数涨幅」改为参考跟踪标的【国开10Y活跃利率债】的净价与涨跌幅
    #   （原先为 T2612 国债期货价与涨幅），仅供人工对照，不参与估值。
    if code == '511520':
        try:
            import bond_pcf_valuation as _bpv
            _r = _bpv.estimate_511520_nav()
        except Exception as _e:
            _log_511520_state("exception", "[bond] 511520 PCF 逐券估值失败: %s" % _e)
            _r = None
        if _r and _r.get("est") is not None:
            out["est"] = _r["est"]
            out["idx_chg"] = _r.get("idx_chg")
            if _r.get("index_nm"):
                out["index_nm"] = _r["index_nm"]
                out["idx_price"] = _r.get("idx_price")
            out["method"] = _r.get("method") or "pcf_basket"
            out["pcf_detail"] = _r.get("detail")
            out["algo_id"] = _r.get("algo_id")
            out["algo_name"] = _r.get("algo_name")
            if _r.get("note"):
                out["note"] = _r["note"]
                _log_511520_state("degraded", "[bond] 511520 估值提示: %s" % _r["note"])
            else:
                _log_511520_state("ok")
            return out
        # 取数失败 ⇒ 退化为「票息 + 活跃10Y国债期货方向」（旧口径兜底，写清 note）
        _log_511520_state("fallback",
                          "[bond] 511520 PCF 逐券估值不可用，退化为票息+期货方向旧口径")

    # ══ 511520 兜底：票息 + 活跃 10Y 国债期货方向 ══
    # 🔴 方向信号优先级：活跃 10Y 国债期货（arbTest 口径，新浪，合约按交割月动态取）>
    #   国债指数000012（同向代理，sina 限频时退化，二者同跟踪利率、方向一致）。
    #   确保 sina 跪时仍有方向，不全退化成净值。
    if code == '511520':
        daily_coupon = meta.get('daily_coupon', 0.02)
        _cp = daily_coupon if _is_trading_day(date.today()) else 0.0
        if futures_pct is not None:
            sig = futures_pct
            sig_nm = "10Y国债期货%s" % _active_cffex_code("T")
            out["method"] = "coupon+futures_active"
        elif treasury_pct is not None:
            sig = treasury_pct
            sig_nm = "国债指数(代理)"
            out["method"] = "coupon+treasury_proxy"
        else:
            sig = 0.0
            sig_nm = "国债指数(代理)"
            out["method"] = "coupon_only_no_index"
            # 🔴 源心跳：活跃10Y国债期货(新浪) 与 国债指数000012(代理) 双源皆失效，
            #   511520 退化为纯票息（无方向信号）。记 log 便于发现抓取链路中断。
            logger.warning("[bond] 511520 方向信号双源失效：活跃%s期货与国债指数000012"
                           "均不可得，退化为纯票息估值" % _active_cffex_code("T"))
        futures_adj = latest_nav * sig / 100.0
        # 与 PCF 主口径一致：债券 99.52% 承担票息+方向，逆回购 0.48% 只计息
        try:
            import bond_pcf_valuation as _bpvf
            _cp = _bpvf.static_carry_unit()[0] if _is_trading_day(date.today()) else 0.0
            _bw, _rw = _bpvf.BOND_WEIGHT, _bpvf.REPO_WEIGHT
            _rr, _ = _bpvf.fetch_repo_rate()
        except Exception:
            _bw, _rw, _rr = 1.0, 0.0, 0.0
        _repo_days = 1 if _is_trading_day(date.today()) else 0
        est = (latest_nav + _bw * (_cp + futures_adj)
               + _rw * latest_nav * _rr * _repo_days / 365.0)
        out["est"] = round(est, 4)
        out["idx_chg"] = sig
        out["index_nm"] = sig_nm
        return out

    # ══ 511880 / 511990 / 其他货币型：日均增长法（每个自然日都计息）══
    avg = calc_avg_daily_growth(history, meta.get('weekend_on'), days=20)
    out["method"] = "avg_daily_growth"
    if avg is None:
        out["est"] = latest_nav
        out["method"] = "latest_only"
        return out
    try:
        last_dt = datetime.strptime(latest_date, "%Y-%m-%d")
    except ValueError:
        out["est"] = latest_nav
        return out
    est = latest_nav
    cur = last_dt + timedelta(days=1)
    while cur.strftime("%Y-%m-%d") <= today:
        est += avg          # 货币型每个自然日都计息（含周末/节假日）
        cur += timedelta(days=1)
    out["est"] = round(est, 4)
    out["idx_chg"] = None
    out["index_nm"] = None
    return out


# ════════════════════════════════════════════════════════════════════
# 静态估值历史（「静态估值」列 + 点击弹核对页，2026-09-19 东哥要求对齐 LOF 程序）
#
# 口径与 LOF 的 static_val 引擎同构（公布 nav(D) 之前的预估，随后核对答案）：
#   static_val(D) = nav(D-1) + 票息 + 方向项(D)
#     · 511880：nav(D-1) + 日均增量 × 计息天数(D-1→D，周末效应同日均增长法)
#     · 511360：nav(D-1) + 0.003 + 0.07 × 国债指数000012日涨幅%
#     · 511520：nav(D-1) + 0.02 + nav(D-1) × 方向%(D)/100
#       方向：活跃 10Y 国债期货日K（新浪 InnerFuturesNewService，按估值日动态取合约）优先；
#       缺失日回退国债指数000012 同向代理 —— 与实时估值的退化优先级完全一致。
#   err_pct = (nav(D) ÷ static_val(D) − 1) × 100
# 净值源 = ETFarb 自有 `fund_nav_history`（腾讯 year_all，174 行/只）——
# 单一事实源，不再走天天基金（避免双源数字打架）。
# ════════════════════════════════════════════════════════════════════
_FUT_KLINE_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
                  "var%20t=/InnerFuturesNewService.getDailyKLine?symbol={sym}")
_SINA_KLINE_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20d=/"
                   "CN_MarketDataService.getKLineData?symbol={sym}&scale=240&ma=no&datalen=200")
# 🔴 上面两个 URL 模板含 `%20`，绝不能用 `url % code` 做替换——`%20t`/`%20d`
#   会被 Python 当格式符抛 TypeError（2026-09-19 踩坑：K 线静默返回空）。统一 .format()。
_fut_kline_cache = {"t": 0.0, "v": None}
_fut_kline_lock = threading.Lock()
_tr_kline_cache = {"t": 0.0, "v": None}


def _fetch_futures_kline(symbol="T2609", ttl=1800):
    """T2609 日K收盘 {date: close}。进程内缓存 30min；失败返回 {}。"""
    now = time.monotonic()
    with _fut_kline_lock:
        if _fut_kline_cache["v"] is not None and symbol in _fut_kline_cache["v"] \
                and now - _fut_kline_cache["t"] < ttl:
            return _fut_kline_cache["v"][symbol]
    out = {}
    try:
        req = urllib.request.Request(_FUT_KLINE_URL.format(sym=symbol), headers={
            "Referer": "https://finance.sina.com.cn/", "User-Agent": _UA})
        raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
        s = raw.find("(["); e = raw.rfind("])")
        if s >= 0 and e > s:
            for it in json.loads(raw[s + 1:e + 1]):
                try:
                    out[str(it["d"])] = float(it["c"])
                except (ValueError, TypeError, KeyError):
                    continue
    except Exception:
        pass
    with _fut_kline_lock:
        # 🔴 只在成功取到数据时写缓存，避免新浪限频返回的空 {} 污染 30min 缓存
        #    （否则一次限频会让后续 30 分钟内 511520 弹窗全退化成国债指数代理）。
        if out:
            if _fut_kline_cache["v"] is None:
                _fut_kline_cache["v"] = {}
            _fut_kline_cache["v"][symbol] = out
            _fut_kline_cache["t"] = now
    return out


def _fetch_treasury_kline_sina():
    """国债指数000012 日K（新浪 CN_MarketDataService，200根）。进程内缓存 30min。

    腾讯 fqkline 2026-09-19 晚实测对全部 A 股指数 501（临时限频？）⇒ 债券方向
    历史不能等它；新浪兜底并回写 index_history（source='sina_kline'），
    消费侧仍只认 index_history 一张表。"""
    return _fetch_sina_kline("sh000012")


_kline_cache = {}                       # {sym: {"t": monotonic, "v": {date: close}}}
_kline_lock = threading.Lock()


def _fetch_sina_kline(sym, ttl=1800):
    """通用新浪日K收盘 {date: close}（基金/指数都走这个口），缓存 30min。"""
    now = time.monotonic()
    with _kline_lock:
        hit = _kline_cache.get(sym)
        if hit and now - hit["t"] < ttl:
            return hit["v"]
    out = {}
    try:
        req = urllib.request.Request(_SINA_KLINE_URL.format(sym=sym), headers={
            "Referer": "https://finance.sina.com.cn/", "User-Agent": _UA})
        raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
        s = raw.find("(["); e = raw.rfind("])")
        if s >= 0 and e > s:
            for it in json.loads(raw[s + 1:e + 1]):
                try:
                    out[str(it["day"])[:10]] = float(it["close"])
                except (ValueError, TypeError, KeyError):
                    continue
    except Exception:
        pass
    with _kline_lock:
        # 🔴 只在取到数据时写缓存，避免限频空结果污染 30min 缓存
        if out:
            _kline_cache[sym] = {"t": now, "v": out}
    return out


def fetch_fund_kline(code, ttl=1800):
    """场内基金日收盘 {date: close}（新浪 CN_MarketDataService）。

    沪/深前缀未知 ⇒ 先试 sh 再试 sz，取非空者（LOF 沪深都有）。"""
    for pfx in ("sh", "sz"):
        rows = _fetch_sina_kline(pfx + code, ttl)
        if rows:
            return rows
    return {}


def clear_caches():
    """「核对静态估值（补采+重算）」入口：清空全部进程内数据缓存，强制回源。"""
    with _kline_lock:
        _kline_cache.clear()
        _fut_kline_cache["t"] = 0.0
        _fut_kline_cache["v"] = None
    _tr_kline_cache["t"] = 0.0
    _tr_kline_cache["v"] = None
    with _nav_cache._lock:
        _nav_cache._nav.clear()
    # 2026-11-14：两个独立估值模块的日级缓存一并清掉，保证「补采+重算」真回源
    # 2026-11-17：新增现金债 ETF 模块（中证指数日线缓存）一并清
    for _mod in ("bond_pcf_valuation", "bond_511360_valuation",
                 "bond_sse_pcf_valuation", "bond_cash_etf_valuation",
                 "bond_cb_etf_valuation"):
        try:
            __import__(_mod).clear_caches()
        except Exception:
            pass


def _treasury_series(db=None):
    """国债指数000012 日收盘 {date: close}（ETFarb `index_history` 单一事实源）。

    1) 先按 static_val 通道过期即补（腾讯 fqkline，12h TTL）；
    2) 库内仍 <2 行 ⇒ 新浪日K兜底并 INSERT OR REPLACE 回写 index_history；
    3) 再读库。两步都空 ⇒ {}（上层宁空勿错）。"""
    import sqlite3
    try:
        import static_val as _sv
        _sv.ensure_index_history(["000012"], db)
    except Exception:
        pass
    own = isinstance(db, str)
    c = sqlite3.connect(db) if own else db

    def _read():
        return {d: v for d, v in c.execute(
            "SELECT date, close FROM index_history WHERE symbol='000012' ORDER BY date")}

    try:
        rows = _read()
        if len(rows) < 2:
            sina = _fetch_treasury_kline_sina()
            if len(sina) >= 2:
                c.executemany(
                    """INSERT OR REPLACE INTO index_history (symbol, date, close, source)
                       VALUES ('000012', ?, ?, 'sina_kline')""",
                    [(d, v) for d, v in sina.items()])
                c.commit()
                rows = _read()
        return rows
    finally:
        if own:
            c.close()


def _accrual_days(d1, d2, weekend_on=None):
    """D-1→D 的计息天数（自然日，货币型每日计息）。weekend_on 保留兼容，不再使用。"""
    return max((d2 - d1).days, 1)


def _close_on_or_before(series, date):
    best = None
    for d in series:
        if d <= date:
            best = d if (best is None or d > best) else best
        else:
            break
    return (best, series[best]) if best else (None, None)


def static_history_bond(code, days=30, db=None, refresh=False):
    """债券型静态估值历史（date 降序），行形状对齐 arbTest 历史核对页：

      {date, nav, nav_chg, price, price_chg, yield_per_wan, premium_pct,
       static_val, val_error, val_error_rate, err_pct,
       idx_close, idx_pct (511360) / futures_close, futures_pct (511520),
       algo_id, algo_name, algo_mode, algo_src, algo_note}

    口径（与 arbTest 历史弹窗同列同名）：
      yield_per_wan = (nav − price) × 100「折价几根毛」
      premium_pct   = (price ÷ nav − 1) × 100「溢价」
      val_error     = static_val − nav「估值误差」
      val_error_rate= (static_val − nav) ÷ nav × 100「误差率」
    🔴 2026-11-14：每行额外记录**实际使用的估值算法**（algo_*），历史页据此标注。
      · 511360：调用独立方法 estimate_511360_nav（PCF逐券篮子 + 三季报结构 + FR007），
        历史日按 Δy=0 的纯票息口径复算，与实时同算法；国债指数只作展示列。
      · 511520：优先 CFETS 逐券估值全价两日变动（与实时 cfets 路径同算法），
        其次本地 PCF 逐日快照真实全价变动，最后才用「逐券carry + 曲线/代理方向」。
      · 511880：日均增长法（未变更）。
    数据不足（净值 <2 行 / 无 PCF 快照也无方向数据）的日期不出行 —— 宁空勿错。"""
    import sqlite3
    if refresh:
        clear_caches()
    meta = BOND_ETF_META.get(code)
    if not meta:
        # 🔴 2026-11-17：现金管理类交易所信用债 ETF（9 只）刻意**不**登记在
        #   BOND_ETF_META —— 它们的估值入口是 bond_cash_etf_valuation，走中证
        #   指数口径，与 BOND_ETF_META 里那套 PCF/逐券/期货完全不同。这里给一个
        #   最小占位，让下面的循环能正常跑（weekend_on 等字段它们用不上）。
        if code in _CASH_BOND_ETF_CODES:
            meta = {"name": code, "cash_bond": True}
        elif code in _CB_ETF_CODES:
            meta = {"name": code, "cb_etf": True}
        else:
            return []
    c = sqlite3.connect(db) if isinstance(db, str) else db
    try:
        navs = [(d, v) for d, v in c.execute(
            "SELECT date, nav FROM unified_fund_history WHERE fund_code=? AND nav>0 "
            "ORDER BY date", (code,))]
    finally:
        if c is not db:
            c.close()
    # 🔴 2026-11-17：现金债 ETF 必须再取 acc（累计净值），用于**剔除分红**。
    #   acc 在库里的口径是「累计净值/100」，故 acc×100 − nav = 累计每份分红。
    #   551500 于 2026-11-16 每份派 1.6 元：nav 102.6844→100.9703（-1.67%），
    #   但累计净值只跌 0.107%。不看 acc 就会把这天当成暴跌，历史页出现
    #   「nav_chg=-1.67%、val_error=+1.71」的假错，读起来像算法错了。
    accs = {}
    if code in _CASH_BOND_ETF_CODES:
        c2 = sqlite3.connect(db) if isinstance(db, str) else db
        try:
            for d, a in c2.execute(
                    "SELECT date, acc FROM unified_fund_history WHERE fund_code=? "
                    "AND acc>0 ORDER BY date", (code,)):
                accs[d] = a
        finally:
            if c2 is not db:
                c2.close()
    # 🔴 2026-11-14 新增的上交所三只（511090/511130/511220）：DB 未同步净值历史时，
    #    直接回退东财 lsjz 实时历史（只读，不写库），保证历史页能出数。
    if len(navs) < 2 and code in ('511090', '511130', '511220'):
        navs = sorted(_nav_cache.get_history(code, days=days + 10))
    # 现金债 ETF 同理：DB 里净值已齐（209 行/只），但保险起见留一条东财兜底
    if len(navs) < 2 and code in _CASH_BOND_ETF_CODES:
        navs = sorted(_nav_cache.get_history(code, days=days + 10))
    # 可转债 ETF 同理（这两只不在库里时也能出数）
    if len(navs) < 2 and code in _CB_ETF_CODES:
        try:
            import bond_cb_etf_valuation as _cbev
            _m = _cbev.fetch_nav_long(code, pages=3)
            navs = sorted((d, v[0]) for d, v in _m.items() if v[0])
        except Exception:
            navs = []
    if len(navs) < 2:
        return []
    dates = [d for d, _ in navs]
    weekend_on = meta.get('weekend_on')

    # 收盘价序列（一次性）：{date: (close, chg%)}，chg 相对上一K线日
    px_raw = fetch_fund_kline(code)
    px_dates = sorted(px_raw)
    px = {}
    for j, pd in enumerate(px_dates):
        prev_px = px_raw[px_dates[j - 1]] if j > 0 and px_raw[px_dates[j - 1]] else None
        px[pd] = (px_raw[pd],
                  (px_raw[pd] / prev_px - 1) * 100 if prev_px else None)

    # 方向序列（一次性取好）
    # 511520 之外的 code 也可能走到 else 分支，先给票息/仓位安全默认值
    _carry_unit = BOND_ETF_META.get('511520', {}).get('daily_coupon', 0.0064)
    _bw, _rw, _repo_r = 1.0, 0.0, 0.0
    tr = {}
    _b360 = None
    pcf360_series, frr_series = {}, {}
    cfets_static = {}
    _csi_series = {}
    if code in _CASH_BOND_ETF_CODES:
        # 🔴 2026-11-17：现金管理类「交易所信用债 ETF」历史口径。
        #   驱动 = 中证指数官方日涨跌（财富指数，涨幅已含票息，故不叠 carry）。
        #   实盘回测（19 个交易日，static=nav(D-1)×(1+指数(D)涨幅) vs 实际 nav(D)）：
        #     511070 0.41bp / 551900 0.31bp / 159600 0.48bp / 551030 0.27bp（中位）。
        #   一次取好整段指数日线，循环里纯查表，几百行也秒出。
        #   ⚠️ 必须延迟导入：bond_cash_etf_valuation 在模块级 import 了本模块，
        #      顶层再反向导入会成环。
        try:
            import bond_cash_etf_valuation as _cbe
            _idx = (_cbe.CASH_BOND_ETF_META.get(code) or {}).get("idx")
            if _idx:
                _csi_series = _cbe.fetch_csi_series(_idx)
        except Exception as e:
            logger.debug("[bond] %s 现金债ETF指数日线不可用: %s", code, e)
            _csi_series = {}
    _cb_navpct = {}
    if code in _CB_ETF_CODES:
        # 🔴 2026-11-17 晚（请求 H）：可转债 ETF 历史口径。
        #   驱动 = 转债指数日涨跌 × beta（511380→931078、511180→000139）。
        #   🔴 必须**逐日用滚动 beta**，不能拿全样本 beta 回算全历史 ——
        #      那是前视偏差（511180 的 beta 从 0.686 漂到 0.817，用最终值回算
        #      早期会把误差做小，等于自欺）。下面循环里按「截至当日之前」回归。
        #   同样必须延迟导入（bond_cb_etf_valuation 模块级 import 了本模块）。
        try:
            import bond_cb_etf_valuation as _cbev
            _m = _cbev.CB_ETF_META.get(code) or {}
            if _m.get("idx"):
                _csi_series = _cbev._cbe.csi_series_long(_m["idx"], segments=3)
            _nm = _cbev.fetch_nav_long(code, pages=3)
            _cb_navpct = dict(_cbev._cum_pct_series(_nm))
        except Exception as e:
            logger.debug("[bond] %s 可转债ETF指数/净值不可用: %s", code, e)
            _csi_series, _cb_navpct = {}, {}
    if code == '511360':
        # 国债指数 000012 只用于「指数 / 指数涨幅」展示列，不再参与估值；
        # 取不到也不再让整页空掉（估值已改由独立 PCF 方法负责）。
        tr = _treasury_series(db)
        try:
            import bond_511360_valuation as _b360
            pcf360_series = _b360.load_pcf_series("511360")
            frr_series = _b360.fr007_series()
        except Exception as e:
            logger.debug("[bond] 511360 历史估值数据源不可用: %s", e)
    elif code == '511520':
        # 2026-11-13 起 511520 跟踪标的改为「国开10Y活跃利率债」。
        # 历史方向优先级：
        #   1) 本地逐日 PCF 快照（bond_pcf_valuation.static_val_from_pcf）——
        #      完全由基金公司公布的实际全价变化驱动，零推算，最准；
        #   2) 无 PCF 快照的历史日 → 活跃10Y国债期货日K（旧口径，作为国开10Y的
        #      同向代理，合约按估值日自动换月）；
        #   3) 期货也缺 → 国债指数000012 代理。
        # 每行用 dir_src 标注实际口径，避免把代理数据误认为国开10Y真实价格。
        _cur_cc = _active_cffex_code("T")
        _fetch_futures_kline(_cur_cc)
        tr = _treasury_series(db)          # 代理回退源
        try:
            import bond_pcf_valuation as _bpv
            pcf_static = _bpv.static_val_from_pcf(code)
            cfets_static = _bpv.static_val_from_cfets(code)
        except Exception as e:
            logger.debug("[bond] 511520 PCF/CFETS 静态估值历史不可用: %s", e)
            pcf_static = {}
            cfets_static = {}
        # 🔴 票息与仓位都不再拍脑袋（2026-11-14）：
        #    · 日票息 = 按成份券反推票面利率 → 组合加权 → 折算元/份/日
        #    · 债券 99.52% 承担票息+方向，一日逆回购 0.48% 只按 GC001 计息
        try:
            _carry_unit, _carry_src = _bpv.static_carry_unit()
            _bw, _rw = _bpv.BOND_WEIGHT, _bpv.REPO_WEIGHT
            _repo_r, _ = _bpv.fetch_repo_rate()
            logger.info("[bond] 511520 静态票息口径: %.6f 元/份/日（%s）",
                        _carry_unit, _carry_src)
        except Exception as e:
            logger.debug("[bond] 511520 静态票息计算值不可用，用 meta 兜底: %s", e)
            _carry_unit = BOND_ETF_META['511520']['daily_coupon']
            _bw, _rw, _repo_r = 1.0, 0.0, 0.0

    # 511880：日均增量只依赖全历史，循环外算一次
    avg_511880 = None
    if code == '511880':
        avg_511880 = calc_avg_daily_growth(list(reversed(navs)), weekend_on, days=20)

    out = []
    for i in range(1, len(dates)):
        d, d0 = dates[i], dates[i - 1]
        nav_prev = navs[i - 1][1]
        nav_cur = navs[i][1]
        static = None
        idx_close = idx_pct = fut_close = fut_pct = None
        dir_src = None
        algo = {"id": "unknown", "name": "—", "mode": "—", "src": "—", "note": ""}
        _div_amt = 0.0          # 当日前每份分红（现金债 ETF 专用）
        _adj_prev = nav_prev    # 除息调整后的「有效昨净值」
        if code in _CASH_BOND_ETF_CODES:
            # ── 现金管理类交易所信用债 ETF（2026-11-17 接入）──────────────
            #   驱动 = 中证指数官方日涨跌；指数是**财富指数**，涨幅已含票息，
            #   故不再叠加 carry（叠加会重复计息 +1.0~1.9bp，实测见模块 docstring）。
            #   分红处理：acc×100 − nav 即累计每份分红，当日增量就是本日派现。
            #   估值与涨跌都用「除息调整后」的口径，避免把派现当暴跌。
            if d in accs and d0 in accs:
                _div_amt = ((accs[d] - accs[d0]) * 100.0) - (nav_cur - nav_prev)
                # 🔴 acc 在库里只有 4 位小数（1.0312），×100 后精度仅 0.01 元，
                #   相减会留下 ±1 分的量化噪量（实测 0.0026~0.0081 这种）。
                #   真分红的量级完全不同：这批 ETF 净值在 100 上下，派现率最低也有
                #   50bp（≈0.5 元/份），511110 派过 2.1 元、551500 派过 1.6 元。
                #   故用 0.05 元作门槛：低于它一律判为噪音置 0，高于 5 元判为脏数据。
                if 0.05 <= _div_amt <= 5.0:
                    pass
                else:
                    _div_amt = 0.0
            _adj_prev = nav_prev - _div_amt
            _v = _csi_series.get(d) if _csi_series else None
            if _v and _v.get("pct") is not None and _adj_prev > 0:
                static = _adj_prev * (1.0 + _v["pct"] / 100.0)
                idx_close, idx_pct = _v.get("close"), _v["pct"]
                _idxc = (CASH_BOND_ETF_META_IDX.get(code) or "")
                algo = {"id": "cash_bond_etf_csi_index_v1",
                        "name": "中证指数日涨跌锚定（历史回测）",
                        "mode": "nav(D-1)×(1+指数(D)涨幅)",
                        "src": "中证指数 %s 日线" % _idxc,
                        "note": "指数为财富指数口径，涨幅已含票息故不叠 carry；"
                                "实测（19 个交易日）|误差| 中位 0.27~0.48bp"}
                if _div_amt:
                    algo["note"] += "；当日每份派现 %.4f 元，已按除息口径复算" % _div_amt
        elif code in _CB_ETF_CODES:
            # ── 可转债 ETF（2026-11-17 晚，请求 H）──────────────────────
            #   驱动 = 转债指数日涨跌 × **截至当日前**的滚动 beta。
            #   🔴 逐日回归、绝不用全样本 beta：511180 的 beta 在 4 个月内从
            #      0.686 漂到 0.817，拿最终值回算早期等于用未来信息，会把
            #      误差做小（前视偏差）。实测样本外中位：511380 1.2bp、511180 7.3bp。
            #   可转债没有「票息 carry」项 —— 利息已含在转债全价里。
            _v = _csi_series.get(d) if _csi_series else None
            if _v and _v.get("pct") is not None and nav_prev > 0:
                # 只用 d 之前（不含 d）的样本回归 beta，避免用当日结果预测当日
                _prior = [x for x in sorted(_cb_navpct) if x < d
                          and x in _csi_series
                          and _csi_series[x].get("pct") is not None]
                _prior = _prior[-_CB_BETA_LOOKBACK:]
                _b = None
                _b_note = ""
                if len(_prior) >= _CB_BETA_MIN:
                    _X = [_csi_series[x]["pct"] for x in _prior]
                    _Y = [_cb_navpct[x] for x in _prior]
                    _n = len(_X)
                    _mx = sum(_X) / _n
                    _my = sum(_Y) / _n
                    _var = sum((x - _mx) ** 2 for x in _X)
                    if _var > 1e-12:
                        _b = (sum((_X[i] - _mx) * (_Y[i] - _my) for i in range(_n))
                              / _var)
                        _b_note = "滚动%d日回归(n=%d)" % (_CB_BETA_LOOKBACK, _n)
                if _b is None:
                    _b = _CB_ETF_BETA_FIX.get(code, 1.0)
                    _b_note = "样本不足，用长样本固定 beta"
                static = nav_prev * (1.0 + _b * _v["pct"] / 100.0)
                idx_close, idx_pct = _v.get("close"), _v["pct"]
                algo = {"id": "cb_etf_index_beta_v1",
                        "name": "转债指数×滚动beta（历史回测）",
                        "mode": "nav(D-1)×(1+beta×指数(D)涨幅)",
                        "src": "中证指数 %s 日线；beta=%s"
                               % (CB_ETF_META_IDX.get(code, ""), _b_note),
                        "note": ("可转债无票息项（利息含在转债全价）；"
                                 + ("实测样本外 |误差| 中位 1.2bp、max 4.4bp，可当精确值"
                                    if code == "511380" else
                                    "实测样本外 |误差| 中位 7.3bp、max 42.4bp —— "
                                    "仅持85只、银行转债占比高、beta 在漂移，只能当参考"))}
        elif code == '511880':
            if avg_511880 is None:
                continue
            try:
                accr = _accrual_days(datetime.strptime(d0, "%Y-%m-%d"),
                                     datetime.strptime(d, "%Y-%m-%d"), weekend_on)
            except ValueError:
                continue
            static = nav_prev + avg_511880 * accr
            algo = {"id": "511880_avg_growth_v1", "name": "日均增长法",
                    "mode": "近20日日均增量×计息天数", "src": "官方净值历史",
                    "note": "货币型每日计息（含周末/节假日）"}
        elif code == '511360':
            # 国债指数只作参考展示（取不到留空，不再影响估值出行）
            if len(tr) >= 2:
                b_d, b_px = _close_on_or_before(tr, d0)
                c_d, c_px = _close_on_or_before(tr, d)
                if b_px and c_px:
                    idx_close, idx_pct = c_px, (c_px / b_px - 1) * 100
            # 🔴 2026-11-14：调用独立估值方法复算（与实时同算法，历史日 Δy=0）。
            #    全部数据注入 ⇒ 不触发任何网络请求，历史页几百行也秒出。
            if _b360 is not None and pcf360_series:
                cand = [x for x in sorted(pcf360_series) if x <= d]
                pcf360 = pcf360_series[cand[-1]] if cand else None
                if pcf360:
                    try:
                        fr = _b360.fr007_on(d, frr_series)
                        _v = _b360.estimate_511360_nav(
                            nav_info={"nav": nav_prev, "nav_date": d0},
                            pcf=pcf360, spot={}, fr007=fr, target_date=d)
                        if _v.get("est") is not None:
                            static = _v["est"]
                            algo = {"id": _v["algo_id"], "name": _v["algo_name"],
                                    "mode": "Δy=0 纯票息 + FR007（历史回测口径）",
                                    "src": "海富通PCF %s / FR007 %s" %
                                           (pcf360.get("date"), fr),
                                    "note": _v["algo_id"] +
                                            "：PCF逐券篮子 + 债券82.80%/逆回购17.16%"}
                    except Exception as e:
                        logger.debug("[bond] 511360 历史静态估值 %s 失败: %s", d, e)
            if static is None:
                # 无 PCF 快照的极早期历史：旧口径兜底，明确标注（避免与实时口径混淆）
                if len(tr) >= 2 and idx_pct is not None:
                    static = (nav_prev + meta.get('daily_coupon', 0.003)
                              + meta.get('idx_coefficient', 0.07) * idx_pct)
                    algo = {"id": "511360_legacy_coupon_index",
                            "name": "旧口径·票息+国债指数",
                            "mode": "nav(D-1)+0.003+0.07×国债指数涨幅",
                            "src": "国债指数000012",
                            "note": "该日无 PCF 快照，仅作历史占位；实时已不用此算法"}
        elif code == '511520':  # —— 与实时算法一致的三层优先级
            #   1) CFETS 逐券估值全价两日变动（第三方估值口径，已含应计，零推算）
            #   2) 本地 PCF 逐日快照的真实全价变动（pcf_real）
            #   3) 逐券 carry + 曲线/期货/国债指数代理方向（99.52% / 0.48%）
            pct = None
            _cf = cfets_static.get(d)
            if _cf:
                # 🔴 2026-11-21 修复：CFETS 缓存日可能断档（如 11-16→11-20，缺 17/18/19）。
                #   _cf["ratio"] 是「prev_day→D」的累计全价变动，跨度可能是多日；
                #   原实现却固定乘 nav_prev=nav(D-1)（只有 1 日跨度），把 4 日涨幅套到
                #   1 日净值上 → 11-20 虚高 0.45 元（+37.8bp，static=119.9966 vs nav=119.5447）。
                #   修正：净值基准改用 nav(prev_day)、计息天数改用 prev_day→D 真实跨度。
                _base_day = _cf.get("prev_day") or d0
                _nav_base = dict(navs).get(_base_day, nav_prev)
                try:
                    _cd = _accrual_days(datetime.strptime(_base_day, "%Y-%m-%d"),
                                        datetime.strptime(d, "%Y-%m-%d"), weekend_on)
                except ValueError:
                    _cd = 1
                static = (_bw * _nav_base * _cf["ratio"]
                          + _rw * _nav_base * (1 + _repo_r * _cd / 365.0))
                dir_src = "cfets_real"
                algo = {"id": _cf["algo_id"], "name": _cf["algo_name"],
                        "mode": "CFETS逐券估值全价两日变动（实时同算法）",
                        "src": _cf["src"],
                        "note": "估值全价已含应计利息，债券部分不叠加carry；"
                                "逆回购0.48%%按GC001计息；"
                                "基准=nav(%s)、跨度%d日" % (_base_day, _cd)}
            _ps = pcf_static.get(d)
            if static is None and _ps is not None and _ps.get("static_val"):
                static = _ps["static_val"]
                dir_src = "pcf_real"
                fut_close = _ps.get("active_price")
                fut_pct = _ps.get("active_pct")
                algo = {"id": "511520_pcf_snapshot_real",
                        "name": "PCF逐日快照·真实全价变动",
                        "mode": "相邻两日PCF替代金额比值（零推算）",
                        "src": "本地PCF快照",
                        "note": "基金公司公布的实际全价变化驱动"}
            if static is None:
                cc = _active_cffex_code("T", as_of=datetime.strptime(d, "%Y-%m-%d").date())
                fut = _fetch_futures_kline(cc)          # 按合约缓存，逐日复用
                if d in fut:
                    prev_fd = [x for x in sorted(fut) if x < d]
                    if prev_fd and fut[prev_fd[-1]] > 0:
                        pct = (fut[d] / fut[prev_fd[-1]] - 1) * 100
                        fut_close, fut_pct = fut[d], pct
                        dir_src = "futures_proxy"
                if pct is None and len(tr) >= 2:
                    b_d, b_px = _close_on_or_before(tr, d0)
                    c_d, c_px = _close_on_or_before(tr, d)
                    if b_px and c_px:
                        pct = (c_px / b_px - 1) * 100
                        dir_src = "treasury_proxy"
                if pct is None:
                    continue
                # 无第三方估值的历史日：债券 99.52% → 逐券carry + 方向项；
                # 逆回购 0.48% → 只赚回购息
                try:
                    _cd = _accrual_days(datetime.strptime(d0, "%Y-%m-%d"),
                                        datetime.strptime(d, "%Y-%m-%d"), weekend_on)
                except ValueError:
                    _cd = 1
                static = (nav_prev
                          + _bw * (_carry_unit * _cd + nav_prev * pct / 100.0)
                          + _rw * nav_prev * _repo_r * _cd / 365.0)
                algo = {"id": "511520_carry_direction_v2",
                        "name": "逐券carry+曲线/代理方向",
                        "mode": "99.52%×(逐券carry+方向) + 0.48%×GC001",
                        "src": "方向=" + (dir_src or "none"),
                        "note": "日票息由成份券反推（非固定6.5厘）；"
                                "方向为同向代理，非国开10Y真实价格"}
        elif code in ('511090', '511130'):
            # 30Y 国债ETF：历史方向 = 活跃 TL 期货日K涨跌×TL_RATIO + 组合票息。
            #   TL 合约按估值日自动换月；ratio 与实时估值同源（bond_sse_pcf_valuation）。
            try:
                nd = (datetime.strptime(d, "%Y-%m-%d")
                      - datetime.strptime(d0, "%Y-%m-%d")).days
            except ValueError:
                nd = 1
            cc = _active_cffex_code("TL", as_of=datetime.strptime(d, "%Y-%m-%d").date())
            fut = _fetch_futures_kline(cc)
            if d in fut:
                prev_fd = [x for x in sorted(fut) if x < d]
                if prev_fd and fut[prev_fd[-1]] > 0:
                    pct = (fut[d] / fut[prev_fd[-1]] - 1) * 100
                    fut_close, fut_pct = fut[d], pct
                    dir_src = "tl_proxy"
                    carry = nav_prev * _SSE_Y30 / 365.0 / 100.0 * nd
                    static = nav_prev * (1 + pct / 100.0 * _SSE_TL_RATIO) + carry
                    algo = {"id": "bond_sse_pcf_v1",
                            "name": "TL期货代理+组合票息",
                            "mode": "nav(D-1)×(1+TL涨跌×%.2f) + 票息(%.2f%%年化)"
                                   % (_SSE_TL_RATIO, _SSE_Y30),
                            "src": "30年国债期货%s 日K" % cc,
                            "note": "历史回测口径；实时优先活跃券Δy，此处统一TL代理"}
            if static is None:
                continue
        elif code == '511220':
            # 城投债ETF：历史无逐日银行间信用 Δy 序列 → 纯票息口径
            #   （信用利差日变动小，票息是主要驱动；明确标注不冒充精确值）。
            try:
                nd = (datetime.strptime(d, "%Y-%m-%d")
                      - datetime.strptime(d0, "%Y-%m-%d")).days
            except ValueError:
                nd = 1
            static = nav_prev * (1 + _SSE_Y_CREDIT / 365.0 / 100.0 * nd)
            dir_src = "credit_carry_only"
            algo = {"id": "bond_sse_pcf_v1",
                    "name": "信用纯票息（历史回测）",
                    "mode": "nav(D-1)×(1+%.2f%%/365×天数)" % _SSE_Y_CREDIT,
                    "src": "银行间城投债收益率中位数（当前值近似）",
                    "note": "实时用城投/中票Δy代理；历史日无逐日Δy序列，纯票息复算"}
        if static is None or static <= 0:
            continue
        p = px.get(d, (None, None))
        price, price_chg = (p if p else (None, None))
        # 分红除权日：二级市场价格同样除息下跌，价格涨跌要用除息前价作基准，
        # 否则「价格涨幅」列会显示 -1.6% 的假跌（比净值列更容易被误读成暴跌）。
        if _div_amt and price and price_chg is not None:
            _pf = price - _div_amt
            if _pf > 0:
                price_chg = (price / _pf - 1) * 100
        row = {
            "date": d, "nav": nav_cur,
            "nav_chg": ((nav_cur / _adj_prev - 1) * 100 if _adj_prev else None),
            "price": price, "price_chg": price_chg,
            "yield_per_wan": (nav_cur - price) * 100 if (nav_cur and price) else None,
            "premium_pct": (price / nav_cur - 1) * 100 if (nav_cur and price) else None,
            "static_val": round(static, 4),
            "val_error": static - nav_cur,
            "val_error_rate": (static - nav_cur) / nav_cur * 100 if nav_cur else None,
            "err_pct": (nav_cur / static - 1) * 100,
            # 🔴 记录该行实际采用的估值算法（历史页「估值算法」列）
            "algo_id": algo["id"], "algo_name": algo["name"],
            "algo_mode": algo["mode"], "algo_src": algo["src"],
            "algo_note": algo["note"],
        }
        if code == '511360':
            row["idx_close"], row["idx_pct"] = idx_close, idx_pct
        if code == '511520':
            # 2026-11-13：跟踪标的为国开10Y活跃利率债。
            #   cfets_real     = CFETS逐券估值全价两日变动（与实时估值同算法）
            #   pcf_real       = 本地PCF快照的真实全价变化（最准，含活跃券净价）
            #   futures_proxy  = 活跃10Y国债期货（国开10Y同向代理，合约按月换月）
            #   treasury_proxy = 国债指数000012（二级代理）
            row["futures_close"], row["futures_pct"] = fut_close, fut_pct
            row["dir_src"] = dir_src or "none"
            row["drv_name"] = "国开10Y活跃利率债"
        if code in ('511090', '511130'):
            row["futures_close"], row["futures_pct"] = fut_close, fut_pct
            row["dir_src"] = dir_src or "none"
            row["drv_name"] = "30年国债期货TL(参考)"
        if code == '511220':
            row["dir_src"], row["drv_name"] = dir_src or "none", "银行间城投债(代理)"
        if code in _CASH_BOND_ETF_CODES:
            row["idx_close"], row["idx_pct"] = idx_close, idx_pct
            row["drv_name"] = CASH_BOND_ETF_META_NM.get(code)
            row["div_amt"] = round(_div_amt, 4) if _div_amt else None
        if code in _CB_ETF_CODES:
            row["idx_close"], row["idx_pct"] = idx_close, idx_pct
            row["drv_name"] = CB_ETF_META_IDX.get(code)
        out.append(row)
    return list(reversed(out))[:days]


def latest_static_bond(code, db=None):
    """看板行用：最新一日债券静态估值。返回 dict 或 None。"""
    rows = static_history_bond(code, days=2, db=db)
    return rows[0] if rows else None


if __name__ == "__main__":
    import sys
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
    tz = None
    tf = None
    try:
        from index_quote import IndexQuoteProvider
        tz = IndexQuoteProvider().get_change("000012")
        tz = tz["pct"] if tz else None
    except Exception as e:
        print("treasury idx err", e)
    tf, _ = get_treasury_futures_pct()
    for c in ["511880", "511360", "511520"]:
        print(c, estimate_bond_nav(c, tz, tf))
    print("\n==== static history (last 5) ====")
    import os
    import db as _dbm   # noqa: E402  单库方案 arb_master.db
    _db = _dbm.DB_PATH
    for c in ["511880", "511360", "511520"]:
        rows = static_history_bond(c, days=5, db=_db)
        print(c, "rows=%d" % len(rows))
        for r in rows[-3:]:
            print("   %s nav=%.4f static=%.4f err=%+.3f%%" %
                  (r["date"], r["nav"], r["static_val"], r["err_pct"]))
