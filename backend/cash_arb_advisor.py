# -*- coding: utf-8 -*-
"""
backend/cash_arb_advisor.py —— 货币 / 短融 ETF 折溢价套利机会分析器
====================================================================

被 `backend/strategy_cash_alert.py` 调用，专门服务于 511880 / 511990 / 511360 三只
「现金管理工具」。在原生的「实时溢价超阈值」提醒之外，新增一层**套利可行性判定**：

    1. 折价套利（场内买入 + 赎回）
       只有当「折价收益 + 持有票息」在**实际资金占用天数**内跑赢同期 GC001 逆回购
       才值得做。占用天数随「周几 / 节假日 / 赎回到账 T+N」变化——周五赎回要跨
       周末、节前赎回要跨长假，成本完全不同。

    2. 溢价套利（申购 + 场内卖出）
       申购不是实时到账，T+1/T+2 才能卖。这段空窗里价格会波动，所以要求
       「溢价 ≥ 同期逆回购收益 × 溢价安全倍数(1.5)」才提醒——否则溢价一回落，
       到手收益就和逆回购差不多了，白冒风险。若将来某渠道支持实时到账(T+0)，
       则只要溢价覆盖交易成本即可提醒。

    3. 到账时间 / 额度如实告知
       赎回给出**具体到账日历日 + 时段**；申赎额度尽力抓取东财，取不到就明确
       提示「请在券商 APP 核对」，绝不编造额度数字。

设计原则
--------
* 纯分析、无副作用（除额度抓取的进程内日缓存），可独立 import / 单测。
* 复用项目现成的交易日历（bond_etf_valuation）与 GC001 利率（bond_pcf_valuation），
  取不到时降级但绝不中断提醒主流程。
* 所有参数集中在 ARB_RULES，来源在注释里写明，便于东哥按实际券商规则微调。

用法
----
    from cash_arb_advisor import analyze
    res = analyze(rows)   # rows = ETFMarketBoard 的行（含 price/iopv/iopv_prem）
    analysis = res["511880"]  # 见 analyze() 文档
"""

from __future__ import print_function

import datetime
import os
import re
import ssl
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
_CORE = os.path.join(_PROJ, "core")
for _p in (_HERE, _CORE, _PROJ):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# 参数表（来源见注释；可按自己券商规则微调）
# ---------------------------------------------------------------------------
# daily_coupon_bp：每日票息，单位 bp（万分之一）。来源：项目内估值模块注释 + 博主文章
#   511880 银华日利：日增约 0.0030 元 / 净值约 100 → 万 0.30
#   511990 华宝添益：每万元每日约 0.27 元        → 万 0.27
#   511360 短融 ETF：日均净值增量约 万 0.6~0.7   → 万 0.60（取保守）
# redeem_settle_td / redeem_time：赎回到账交易日数 + 时段。来源：strategy_cash_alert.NOTES
#   （东哥 2026-11-05 定稿）
# purchase_sellable_td：申购后 T+N 交易日可卖。0 = 实时到账。
#   货基/短融场内申购多为 T+1 确认、T+2 可卖；按此保守设定。
# trade_cost_bp：买卖/申赎佣金 + 滑点预留（这三只多券商 0 费率，留 0.5bp 覆盖滑点）
# min_apply_shares / min_apply_amount：一级市场最小申赎单位与对应金额（门槛）。
#   来源：上交所申赎清单 + 基金公司官网（2026-11-18~20），申赎单位极少变动，作静态配置。
#   金额 = 最小申赎单位(份) × 份额净值；净值是浮动的，故金额标注「约」，仅作门槛提示。
#   511880 银华日利：10 份 × 净值约 100.98 ≈ 1,010 元
#   511990 华宝添益： 1 份 × 净值 100.00  =   100 元
#   511360 短融ETF ：2000 份 × 净值约 114.18 ≈ 22.8 万元（门槛高，重点提示）
ARB_RULES = {
    "511880": {
        "name": "银华日利ETF", "type": "货币ETF",
        "daily_coupon_bp": 0.30,
        "redeem_settle_td": 1, "redeem_time": "13:30–14:00",
        "purchase_sellable_td": 2,
        "min_apply_shares": 10, "min_apply_amount": "约1,010元",
        "redeem_path": "场内基金 → 基金赎回 → 代码511880 → 输入份额（15:00 前，赎回后不可撤单）",
        "purchase_path": "场内基金 → 基金申购 → 代码511880 → 输入份额（15:00 前）",
    },
    "511990": {
        "name": "华宝添益ETF", "type": "货币ETF",
        "daily_coupon_bp": 0.27,
        "redeem_settle_td": 1, "redeem_time": "15:00–16:00",
        "purchase_sellable_td": 2,
        "min_apply_shares": 1, "min_apply_amount": "100元",
        "redeem_path": "场内基金 → 基金赎回 → 代码511990 → 输入份额（15:00 前）；到账偏晚，注意三方出金时间",
        "purchase_path": "场内基金 → 基金申购 → 代码511990 → 输入份额（15:00 前）",
    },
    "511360": {
        "name": "短融ETF海富通", "type": "短融债券ETF",
        "daily_coupon_bp": 0.60,
        "redeem_settle_td": 2, "redeem_time": "11:30–12:00 或 13:30–14:00",
        "purchase_sellable_td": 2,
        "min_apply_shares": 2000, "min_apply_amount": "约22.8万元",
        "redeem_path": "场内基金 → 基金赎回 → 代码511360 → 输入份额（15:00 前）；按 T 日净值结算（小数点后 4 位）",
        "purchase_path": "场内基金 → 基金申购 → 代码511360 → 输入份额（15:00 前）",
    },
}

PREMIUM_SAFETY = 1.5      # 溢价套利安全倍数：溢价需 ≥ 逆回购收益 × 1.5（T+1/T+2 到账）
TRADE_COST_BP = 0.50      # 交易成本预留（bp）
MIN_NET_BP = 0.05         # 折价套利最小净收益门槛（bp），过滤噪音

# ---------------------------------------------------------------------------
# 交易日历（复用项目现成实现）
# ---------------------------------------------------------------------------
def _calendar():
    """返回 (_is_trading_day, holidays, makeup)，失败返回 None。"""
    try:
        import bond_etf_valuation as bev
        return bev._is_trading_day, bev._CN_HOLIDAYS_2026, bev._CN_MAKEUP_2026
    except Exception:
        return None


def _is_td(d):
    cal = _calendar()
    if cal:
        return cal[0](d)
    return d.weekday() < 5     # 降级：仅排除周末


def _add_trading_days(d, n):
    """从 d 之后数 n 个交易日，返回该交易日日期（d 本身不计）。"""
    cur = d
    got = 0
    guard = 0
    while got < n and guard < 60:
        cur += datetime.timedelta(days=1)
        guard += 1
        if _is_td(cur):
            got += 1
    return cur


def _next_trading_day(d):
    """d 若为交易日返回 d，否则返回之后第一个交易日。"""
    if _is_td(d):
        return d
    return _add_trading_days(d, 1)


def _occupy_natural_days(t_day, settle_td):
    """T 日操作 → T+settle_td 交易日资金到位，返回占用的自然日天数。"""
    settle_date = _add_trading_days(t_day, settle_td)
    return max(1, (settle_date - t_day).days), settle_date


# ---------------------------------------------------------------------------
# 逆回购利率（复用 GC001）
# ---------------------------------------------------------------------------
def fetch_repo_rate():
    """返回 (年化小数, 来源)。失败降级到 1.5%。"""
    try:
        import bond_pcf_valuation as bpv
        r, src = bpv.fetch_repo_rate()
        if r and 0 < r < 0.5:
            return r, src
    except Exception:
        pass
    return 0.015, "默认年化1.50%"


def _repo_bp(rate, days):
    """占用 days 自然日的逆回购收益（bp）。"""
    return rate * days / 365.0 * 10000.0


# ---------------------------------------------------------------------------
# 申赎额度（尽力抓取 + 降级，每天一次进程内缓存）
# ---------------------------------------------------------------------------
_QUOTA_CACHE = {"date": None, "data": {}}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _http(url, enc="utf-8", timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Referer": "https://fund.eastmoney.com/"})
    return urllib.request.urlopen(req, timeout=timeout, context=_CTX).read().decode(enc, "ignore")


def _fetch_quota_one(code):
    """抓单只基金的申购状态/限额。取不到返回 (None, 说明)。不编造数字。"""
    # 东财基金详情页常含「申购状态：开放申购/限大额/暂停申购」
    try:
        h = _http("https://fund.eastmoney.com/%s.html" % code, "utf-8")
        txt = re.sub(r"<[^>]+>", " ", h)
        txt = re.sub(r"\s+", " ", txt)
        m = re.search(r"申购状态[：:]\s*(开放申购|限大额|暂停申购|暂停交易|认购期|封闭期)", txt)
        status = m.group(1) if m else None
        lim = None
        ml = re.search(r"(?:单日|累计)申购(?:上限|限额)[：:]\s*([\d,.]+\s*[万亿元]?)", txt)
        if ml:
            lim = ml.group(1)
        if status:
            return {"status": status, "limit": lim}, "东财基金详情页"
    except Exception:
        pass
    return None, "东财未提供该场内 ETF 的公开申赎额度接口"


def fetch_quotas(codes=None, force=False):
    """每天抓一次额度/申购状态；返回 {code: {status,limit,src}}，取不到留空并带 note。"""
    today = datetime.date.today().isoformat()
    if not force and _QUOTA_CACHE["date"] == today and _QUOTA_CACHE["data"]:
        return _QUOTA_CACHE["data"]
    codes = codes or list(ARB_RULES)
    out = {}
    for code in codes:
        info, src = _fetch_quota_one(code)
        rec = dict(info or {})
        rec["src"] = src
        out[code] = rec
    _QUOTA_CACHE.update(date=today, data=out)
    return out


# ---------------------------------------------------------------------------
# 主分析
# ---------------------------------------------------------------------------
def analyze(rows, as_of=None, with_quota=True):
    """对三只 ETF 做套利可行性分析。

    rows: ETFMarketBoard 行列表（含 code/price/iopv/iopv_prem/name）
    as_of: 分析基准日（date），默认今天；非交易日顺延到下一交易日
    返回: {"as_of": date, "repo_rate": float, "repo_src": str,
           "items": {code: {...}}, "quota_note": str}
    """
    rate, rsrc = fetch_repo_rate()
    by_code = {str(r.get("code")): r for r in (rows or [])}
    t_day = _next_trading_day(as_of or datetime.date.today())
    quotas = fetch_quotas() if with_quota else {}
    items = {}

    for code, rule in ARB_RULES.items():
        row = by_code.get(code) or {}
        price = _num(row.get("price"))
        iopv = _num(row.get("iopv"))
        prem_pct = _num(row.get("iopv_prem"))
        prem_bp = None if prem_pct is None else prem_pct * 100.0

        # ---------- 折价套利：买入 + 赎回 ----------
        occ_d, settle_date = _occupy_natural_days(t_day, rule["redeem_settle_td"])
        repo_bp_d = _repo_bp(rate, occ_d)
        coupon_bp_d = rule["daily_coupon_bp"] * occ_d
        # 净收益 = 折价幅度 + 持有票息 - 逆回购机会成本 - 交易成本
        # 需折价 > 逆回购成本 - 票息 + 交易成本 + 最小边际
        need_disc = repo_bp_d - coupon_bp_d + TRADE_COST_BP + MIN_NET_BP
        disc_bp = -prem_bp if (prem_bp is not None and prem_bp < 0) else 0.0
        net_bp = (disc_bp + coupon_bp_d - repo_bp_d - TRADE_COST_BP) if prem_bp is not None else None
        disc_ok = prem_bp is not None and prem_bp < 0 and disc_bp >= need_disc
        disc = {
            "feasible": bool(disc_ok),
            "discount_bp": round(disc_bp, 3),
            "need_discount_bp": round(need_disc, 3),
            "net_bp": None if net_bp is None else round(net_bp, 3),
            "occupy_days": occ_d,
            "settle_date": settle_date.isoformat(),
            "settle_weekday": _wd(settle_date),
            "settle_time": rule["redeem_time"],
            "repo_bp": round(repo_bp_d, 3),
            "coupon_bp": round(coupon_bp_d, 3),
            "path": rule["redeem_path"],
        }

        # ---------- 溢价套利：申购 + 卖出 ----------
        sell_td = rule["purchase_sellable_td"]
        realtime = (sell_td == 0)
        occ_p, sellable_date = _occupy_natural_days(t_day, sell_td) if not realtime else (0, t_day)
        repo_bp_p = _repo_bp(rate, occ_p)
        if realtime:
            prem_threshold = TRADE_COST_BP + MIN_NET_BP
            basis = "实时到账，只需覆盖交易成本"
        else:
            prem_threshold = repo_bp_p * PREMIUM_SAFETY + TRADE_COST_BP
            basis = "占用%d自然日逆回购 %.2fbp × %.1f 倍 + 成本" % (
                occ_p, repo_bp_p, PREMIUM_SAFETY)
        prem_ok = prem_bp is not None and prem_bp > 0 and prem_bp >= prem_threshold
        premium = {
            "feasible": bool(prem_ok),
            "premium_bp": round(prem_bp, 3) if (prem_bp is not None and prem_bp > 0) else 0.0,
            "threshold_bp": round(prem_threshold, 3),
            "threshold_basis": basis,
            "realtime": realtime,
            "occupy_days": occ_p,
            "sellable_date": sellable_date.isoformat(),
            "sellable_weekday": _wd(sellable_date),
            "repo_bp": round(repo_bp_p, 3),
            "path": rule["purchase_path"],
        }

        q = quotas.get(code, {})
        # 操作方式判定：折价可行 → 赎回（买入+赎回）；溢价可行 → 申购（申购+卖出）；否则观望
        # 若两者都可行（极少），优先净收益高的一侧
        if disc_ok and prem_ok:
            action, action_kind = ("赎回", "redeem") if (net_bp or 0) >= (prem_bp or 0) - prem_threshold else ("申购", "purchase")
        elif disc_ok:
            action, action_kind = "赎回", "redeem"
        elif prem_ok:
            action, action_kind = "申购", "purchase"
        else:
            action, action_kind = "观望", "wait"

        items[code] = {
            "code": code, "name": row.get("name") or rule["name"], "type": rule["type"],
            "price": price, "iopv": iopv, "prem_bp": None if prem_bp is None else round(prem_bp, 3),
            "discount": disc, "premium": premium,
            "action": action, "action_kind": action_kind,
            "min_apply": {
                "shares": rule.get("min_apply_shares"),
                "amount": rule.get("min_apply_amount"),
            },
            "quota": {
                "status": q.get("status"), "limit": q.get("limit"), "src": q.get("src"),
                "known": bool(q.get("status")),
            },
        }

    any_known = any(it["quota"]["known"] for it in items.values())
    return {
        "as_of": t_day.isoformat(),
        "repo_rate": rate, "repo_src": rsrc,
        "items": items,
        "quota_note": (
            "额度已尽力从东财获取；标注「未获取」的请券商 APP 核对。"
            if any_known else
            "东财未公开此三只场内 ETF 的实时申赎额度；请勿据此直接下单，"
            "务必在券商 APP【场内基金 → 申购/赎回】确认当日额度与是否暂停。"
        ),
    }


def _wd(d):
    return "一二三四五六日"[d.weekday()]


def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 文案（供提醒 markdown 使用）
# ---------------------------------------------------------------------------
def format_rules_markdown(res=None):
    """套利部分的**规则口径**文案，供 strategy_cash_alert 集中到「策略规则」板块。

    这里只放规则/门槛/额度说明，不放任何触发数据，避免与机会明细重复。
    """
    lines = ["- **折溢价套利规则**：折价需跑赢 GC001（净收益 > %.2fbp）；"
             "溢价 ≥ 占用期逆回购 × %.1f 倍 + 交易成本 %.2fbp（T+1/T+2 才可卖）；"
             "不达门槛不提醒\n" % (MIN_NET_BP, PREMIUM_SAFETY, TRADE_COST_BP)]
    if res:
        lines.append("- **当日套利基准**：基准日 %s · GC001 年化 %.3f%%（%s）\n"
                     % (res["as_of"], res["repo_rate"] * 100, res["repo_src"]))
    lines.append("- **申赎额度**：%s\n" % (
        (res or {}).get("quota_note") or
        "东财未公开场内 ETF 实时申赎额度，请在券商 APP【场内基金 → 申购/赎回】核对。"))
    lines.append("- **最小申赎门槛**：%s\n" % "、".join(
        "%s %s份(≈%s)" % (c, r.get("min_apply_shares"), r.get("min_apply_amount"))
        for c, r in ARB_RULES.items()))
    return "".join(lines)


def format_arb_markdown(res):
    """把 analyze() 结果渲染成钉钉 markdown 片段（**仅列有可行机会的标的**）。

    规则口径已抽到 format_rules_markdown()，本函数只输出触发数据本身。
    """
    lines = []
    lines.append("#### 💰 折溢价套利机会（跑赢 GC001 才有意义）\n")
    lines.append("> 基准日 %s · GC001 年化 %.3f%%（%s）\n"
                 % (res["as_of"], res["repo_rate"] * 100, res["repo_src"]))
    any_hit = False
    for code, it in res["items"].items():
        d, p = it["discount"], it["premium"]
        if not (d["feasible"] or p["feasible"]):
            continue
        any_hit = True
        lines.append("**🔴 %s %s（%s）** · 现价 %s / 估值 %s · 溢价 %s bp\n"
                     % (code, it["name"], it["type"], _f(it["price"], 3),
                        _f(it["iopv"], 4), _f(it["prem_bp"], 2)))
        if d["feasible"]:
            lines.append("- ✅ **折价套利（买入+赎回）**：折价 %.2fbp ≥ 需 %.2fbp，"
                         "净赚 ≈ %.2fbp（已扣票息/逆回购/成本）\n"
                         % (d["discount_bp"], d["need_discount_bp"], d["net_bp"] or 0))
            lines.append("  - 占用 %d 自然日（逆回购成本 %.2fbp，票息 +%.2fbp）\n"
                         % (d["occupy_days"], d["repo_bp"], d["coupon_bp"]))
            lines.append("  - 赎回款 **%s（周%s）%s** 到账\n"
                         % (d["settle_date"], d["settle_weekday"], d["settle_time"]))
            lines.append("  - 操作：%s\n" % d["path"])
        if p["feasible"]:
            lines.append("- ✅ **溢价套利（申购+卖出）**：溢价 %.2fbp ≥ 门槛 %.2fbp\n"
                         % (p["premium_bp"], p["threshold_bp"]))
            lines.append("  - 门槛依据：%s\n" % p["threshold_basis"])
            lines.append("  - %s\n" % (
                "申购实时到账，可立即卖出锁定价差" if p["realtime"] else
                "申购 **%s（周%s）** 可卖，之间占用 %d 自然日，注意价格回落"
                % (p["sellable_date"], p["sellable_weekday"], p["occupy_days"])))
            lines.append("  - 操作：%s\n" % p["path"])
        # 操作方式/最小申赎见表格「操作方式/最小申赎」列，额度说明见末尾「策略规则」，
        # 这里只保留触发数据（收益/占用天数/到账日/操作路径），避免重复刷屏。
        lines.append("\n")
    if not any_hit:
        lines.append("> 本轮无跑赢逆回购的折溢价机会（阈值内或无数据）。\n\n")
    return "".join(lines)


def _f(x, nd=2):
    if x is None:
        return "—"
    return ("%." + str(nd) + "f") % x


# ---------------------------------------------------------------------------
# CLI 自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    demo = [
        {"code": "511880", "name": "银华日利ETF", "price": 100.500, "iopv": 100.503, "iopv_prem": -0.0030},
        {"code": "511990", "name": "华宝添益ETF", "price": 100.010, "iopv": 100.000, "iopv_prem": 0.0100},
        {"code": "511360", "name": "短融ETF海富通", "price": 101.200, "iopv": 101.195, "iopv_prem": 0.0049},
    ]
    import json
    r = analyze(demo)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("\n" + format_arb_markdown(r))
