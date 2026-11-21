# -*- coding: utf-8 -*-
"""
backend/strategy_cash_alert.py —— 现金管理 ETF「实时溢价」钉钉提醒策略
=====================================================================

独立策略文件：**与看板解耦、可单独运行**，启动即定时执行，每 5 分钟一轮。

监控标的与触发阈值（东哥口径，单位 bp；1bp = 万分之一 = 0.01%）
------------------------------------------------------------------
| 代码   | 名称           | 触发条件                                   |
|--------|----------------|--------------------------------------------|
| 511880 | 银华日利ETF    | 实时溢价 > +0.5bp  或  实时溢价 < −0.5bp   |
| 511990 | 华宝添益ETF    | 实时溢价 > +0.5bp  或  实时溢价 < −0.5bp   |
| 511360 | 短融ETF海富通  | 实时溢价 > +1.0bp  或  实时溢价 < −1.0bp   |
| 511520 | 政金债ETF富国  | 实时溢价 < 0bp（只报折价，溢价不报）       |
| 511220 | 城投债ETF海富通| 贴水 > 40bp（实时溢价 < −40bp，只报贴水）  |

🔴 2026-11-14 新增「极端波动提醒」
    对 511520/511360/511880/511990/511090/511130/511220 七只：
    当日实时波动 = |实时估值 ÷ 前一日净值 − 1|，基准 = 当年日净值波动的
    绝对值平均（东财 lsjz），实时波动 > 3 × 基准 即触发。
    同一标的同一交易日只提醒一次（去重）。

数据来源（**不新增任何取数逻辑**，全部复用已有代码）
-----------------------------------------------------
1. 优先：本机后端 `GET /api/etf_market?market=B` 与 `?market=C`（`core/etf_iopv_quote.ETFMarketBoard`
   已算好两个市场，后端预热线程维护 30 秒缓存 ⇒ 本策略几乎零额外网络开销）。
   🔴 2026-11-17：16 只债券 ETF 拆到 B、C 仅剩 2 只货币 ETF，监控清单横跨两市场，
   故必须两个都取（ALERT_MARKETS = ("B", "C")），否则债券提醒静默丢失；
2. 回退：后端没起时，直接 `ETFMarketBoard(DB_PATH).build(market=mkt, force_live=True)`
   逐个市场装配（一轮约 4~5 秒，含腾讯行情 + 债券估值 + 腾讯 jj 净值）。

字段映射（沿用看板同名列，语义一致）：
    现价 = row["price"]        实时估值 = row["iopv"]（债券口径 = 日内预估净值）
    实时溢价 = row["iopv_prem"]（**百分数**，如 -0.0073 表示 −0.73bp）
    基金净值 = row["nav"]      净值日期 = row["nav_date"]

钉钉推送
--------
复用 `backend/dingtalk_utils.DingTalkNotifier.send_alert`（内含 12:30–13:00 静默窗口，
该窗口内发送方法返回 False，本策略照此跳过、不重试——保持与既有约定一致）。

用法
----
    # 常驻（每 5 分钟一轮，直到 Ctrl+C）
    python backend/strategy_cash_alert.py

    # 只跑一轮（自测），不发钉钉、只打印消息
    python backend/strategy_cash_alert.py --once --dry-run

    # 只在交易时段内提醒（默认），或全天候提醒
    python backend/strategy_cash_alert.py --all-hours

    # 不看触发条件，每轮都推全量表（纯盯盘用法）
    python backend/strategy_cash_alert.py --always

被 `backend/app.py main()` 调用时走 `start_background()`，作为 daemon 线程挂到看板服务上，
随 `start_dashboard.bat` 一起启动（每 5 分钟自动跑，无需另开窗口）。
"""

from __future__ import print_function

import argparse
import datetime
import json
import os
import sys
import time
import traceback
import urllib.request

# ============ 路径（跨平台自适应，与 backend/app.py 同款） ============
_HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(_HERE)
CORE_DIR = os.path.join(PROJ, "core")
LOG_DIR = os.path.join(PROJ, "log")
LOG_FILE = os.path.join(LOG_DIR, "strategy_cash_alert.log")

for _p in (_HERE, CORE_DIR, PROJ):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _ensure_db_env():
    """直接运行时补 `ARB_MASTER_DB`（与 start_dashboard.bat 同值），否则 core/db.py 会抛错。

    `core/db.py` 的兄弟目录搜索写死目录名 `LOFarb`，而本机实际叫 `lofarb-master`
    ⇒ 独立运行必须显式指定。已有环境变量则完全不动。"""
    if os.environ.get("ARB_MASTER_DB"):
        return
    _arb_root = os.path.dirname(PROJ)
    for _name in ("lofarb-master", "LOFarb"):
        cand = os.path.join(_arb_root, _name, "database", "arb_master.db")
        if os.path.exists(cand):
            os.environ["ARB_MASTER_DB"] = cand
            return
    for _d in (os.path.join(_arb_root, "_shared", "arb_master.db"),):
        if os.path.exists(_d):
            os.environ["ARB_MASTER_DB"] = _d
            return


_ensure_db_env()

# ============ 策略参数 ============
INTERVAL = 300                      # 轮询间隔（秒）= 5 分钟
DEFAULT_API_PORT = 8102             # 与 backend/app.py DEFAULT_PORT 一致
API_TIMEOUT = 20                    # 本机 API 超时（秒）
TRADING_WINDOW = (9 * 60 + 15, 15 * 60 + 5)   # 工作日 09:15–15:05（宽松覆盖集合竞价/尾盘）
TITLE = "债券/现金管理ETF 实时溢价提醒"

# 监控标的：code -> 触发规则
#   mode = "both"  : |溢价| 超阈值（双向都报）
#   mode = "below" : 溢价 < th 才报（th 可为负：511220 贴水>40bp → th=-40）
CTRL_WATCH = {
    "511880": {"name": "银华日利ETF",   "mode": "both",  "th_bp": 0.5},
    "511990": {"name": "华宝添益ETF",   "mode": "both",  "th_bp": 0.5},
    "511360": {"name": "短融ETF海富通", "mode": "both",  "th_bp": 1.0},
    "511520": {"name": "政金债ETF富国", "mode": "below", "th_bp": 0.0},
    "511220": {"name": "城投债ETF海富通", "mode": "below", "th_bp": -40.0},
}

# 🔴 极端波动提醒（2026-11-14）：当日实时波动 > 3 × 当年平均日波动 → 触发
#   当日实时波动 = |实时估值(iopv) ÷ 前一日净值(nav) − 1|
#   当年平均波动 = 当年日净值变动绝对值的平均（东财 lsjz，≥20 个样本才启用）
EXT_VOL_FACTOR = 3.0
EXT_VOL_CODES = {
    "511520", "511360", "511880", "511990",
    "511090", "511130", "511220",
}
_EXT_STATE = {}          # {code: 当日已提醒标记 'YYYY-MM-DD'}（同日去重）
_BASELINE_CACHE = {}     # {code: (ts, 当年平均日波动%)}（6 小时缓存）

# 赎回到账备忘（东哥 2026-11-05 定稿，随每条提醒一并推送）
NOTES = [
    ("511880",
     "目前某河赎回 511880，T+1 交易日到账时间稳定在 **13:30–14:00** 之间。"),
    ("511360",
     "1. 目前某河赎回 511360，T+2 交易日到账时间在中午 **11:30–12:00** 之间，"
     "或者 **13:30–14:00** 之间。\n"
     "2. 511360 的结算，按 **T 日公布的净值**结算，含小数点第 **4** 位。"),
    ("511990",
     "目前某河赎回 511990，T+1 到账时间比较飘忽，**15:00–16:00** 均可能，"
     "请注意且留意你的三方银行出金时间。"),
]

_NOTIFIER = None


# ============ 日志 ============
def _logf(msg):
    """写策略独立日志（不动 server.log，避免与看板日志互相淹没）。"""
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


# ============ 工具 ============
def _num(x, default=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v


def _fmt(x, nd=4):
    v = _num(x)
    return "—" if v is None else ("%." + str(nd) + "f") % v


def _fmt_bp(bp):
    if bp is None:
        return "—"
    return "%+.2f bp" % bp


def _is_trading_session(now=None):
    """工作日 09:15–15:05 视为交易时段。节假日不另判（休市时价格不动，无新信号）。"""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return TRADING_WINDOW[0] <= hm <= TRADING_WINDOW[1]


def _notifier():
    """钉钉通知器懒加载单例（构造不联网，取不到 SDK 时返回 None，不阻断主流程）。"""
    global _NOTIFIER
    if _NOTIFIER is None:
        try:
            from dingtalk_utils import DingTalkNotifier
            _NOTIFIER = DingTalkNotifier()
        except Exception as e:
            _logf("钉钉通知器初始化失败: %s" % e)
            _NOTIFIER = False
    return _NOTIFIER or None


# ============ 取数 ============
# 🔴 2026-11-17：东哥把现金管理里 16 只债券 ETF 拆到独立「债券」模块（market=B），
#    C 只剩 2 只货币 ETF。本策略的监控清单（CTRL_WATCH / EXT_VOL_CODES）横跨 B 和 C，
#    故必须**同时取两个市场并合并**，否则债券 ETF 的溢价提醒会静默丢失。
ALERT_MARKETS = ("B", "C")


def _fetch_from_api(port=None):
    """优先读本机后端缓存（30 秒 TTL，几乎零开销）。失败返回 (None, 原因)。

    2026-11-17 起分别请求 B / C 两市场并合并；只要有一个市场拿到行即算成功。
    """
    port = port or int(os.environ.get("ETFARB_API_PORT") or DEFAULT_API_PORT)
    # 🔴 必须**显式绕过系统代理**：本机常配 http_proxy（如 127.0.0.1:53329），
    #    走代理打 127.0.0.1 会返回 502 Bad Gateway（2026-11-05 实测）。
    #    这里用空 ProxyHandler 直连，与 app.py `_http_get_json` 同款做法。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    all_rows, errs = [], []
    for mkt in ALERT_MARKETS:
        url = "http://127.0.0.1:%d/api/etf_market?market=%s" % (port, mkt)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "etfarb-strategy"})
            with opener.open(req, timeout=API_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            errs.append("%s 不可用: %s" % (mkt, e))
            continue
        all_rows.extend(data.get("rows") or [])
    if not all_rows:
        return None, "本机 API 返回空数据（%s）" % "; ".join(errs or ["无错误"])
    return all_rows, None


def _fetch_direct():
    """后端未运行时直接装配（复用看板同一引擎，绝不另算一套口径）。"""
    import db as _db
    import etf_iopv_quote as eiq
    rows = []
    for mkt in ALERT_MARKETS:
        payload = eiq.ETFMarketBoard(_db.DB_PATH).build(market=mkt, force_live=True)
        rows.extend(payload.get("rows") or [])
    return rows


def fetch_rows(prefer_api=True):
    """取债券(B) + 现金管理(C) 两个市场的看板行合并。返回 (rows, source_desc)。

    🔴 两条路径都出自同一个 `ETFMarketBoard`，字段口径完全一致；先 HTTP 只为省请求。
    """
    if prefer_api:
        rows, err = _fetch_from_api()
        if rows:
            return rows, "api"
        _logf("本机 API 取数失败，回退直算：%s" % err)
    rows = _fetch_direct()
    return rows, "direct"


# ============ 判定 ============
def judge(rows):
    """按阈值判定。返回 (items, hits)。

    items: 每只监控标的的判定明细（含未触发的，供全量表展示）
    hits : 触发的标的列表
    """
    by_code = {str(r.get("code")): r for r in rows}
    items, hits = [], []
    for code, cfg in CTRL_WATCH.items():
        row = by_code.get(code) or {}
        prem_pct = _num(row.get("iopv_prem"))          # 百分数
        prem_bp = None if prem_pct is None else prem_pct * 100.0   # → bp
        th = cfg["th_bp"]
        mode = cfg["mode"]

        if prem_bp is None:
            state, hit = "数据缺失", False
        elif mode == "below":
            hit = prem_bp < th
            state = "折价 %.2fbp < %.2fbp" % (prem_bp, th) if hit else "未达阈值"
        else:
            hit = (prem_bp > th) or (prem_bp < -th)
            if hit:
                state = ("溢价 %+.2fbp > +%.2fbp" % (prem_bp, th) if prem_bp > 0
                         else "折价 %+.2fbp < -%.2fbp" % (prem_bp, th))
            else:
                state = "阈值内(±%.2fbp)" % th

        item = {
            "code": code,
            "name": row.get("name") or cfg["name"],
            "price": _num(row.get("price")),
            "iopv": _num(row.get("iopv")),
            "prem_pct": prem_pct,
            "prem_bp": prem_bp,
            "nav": _num(row.get("nav")),
            "nav_date": row.get("nav_date"),
            "hit": hit,
            "state": state,
            "mode": mode,
            "th_bp": th,
            "err": row.get("err"),
        }
        items.append(item)
        if hit:
            hits.append(item)
    return items, hits


# ============ 极端波动（2026-11-14 新增） ============
def _fetch_nav_year(code, max_pages=14):
    """东财 lsjz 翻页抓「当年」净值序列 [(date, nav)] 升序。

    ⚠️ lsjz 实测每页最多 20 行（pageSize 传大也只回 20），全年 ~220 行
    需翻 ~11 页；翻到当年 1 月 1 日之前即停（不拉多余历史）。
    失败/空页返回已取到的部分（≥20 行即可用）。
    """
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    year = datetime.datetime.now().year
    out = []
    for page in range(1, max_pages + 1):
        url = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery"
               "&fundCode=%s&pageIndex=%d&pageSize=20" % (code, page))
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://fundf10.eastmoney.com/jjz_%s.html" % code})
            raw = urllib.request.urlopen(req, timeout=10, context=ctx).read().decode("utf-8", "ignore")
            s, e = raw.find("{"), raw.rfind("}") + 1
            data = json.loads(raw[s:e]) if s >= 0 else {}
            recs = (data.get("Data") or {}).get("LSJZList") or []
            if not recs:
                break
            stop = False
            for r in recs:
                d, v = r.get("FSRQ"), r.get("DWJZ")
                if not d or not v:
                    continue
                if str(d)[:4] != str(year):
                    stop = True
                    continue
                try:
                    out.append((str(d), float(v)))
                except (TypeError, ValueError):
                    continue
            if stop or len(recs) < 20:
                break
        except Exception:
            break
    return sorted(out)


def _year_baseline(code):
    """当年日净值波动绝对值平均（%）。东财 lsjz → 当年内相邻日 |Δ| 平均。

    返回 None 表示样本不足（<20 行）或取数失败 —— 该标的本轮跳过极端判定。
    缓存 6 小时：净值序列日内不变，无需每 5 分钟重拉。
    """
    now = time.time()
    if code in _BASELINE_CACHE and now - _BASELINE_CACHE[code][0] < 6 * 3600:
        return _BASELINE_CACHE[code][1]
    base = None
    try:
        ys = _fetch_nav_year(code)
        chgs = [abs(ys[i][1] / ys[i - 1][1] - 1) * 100
                for i in range(1, len(ys)) if ys[i - 1][1]]
        # 剔除 >20% 的异常跳变（511990 东财历史尺度错乱：0.28↔0.54↔100 混串，
        # 真实债券/货币 ETF 单日波动不可能 >20%）
        clean = [c for c in chgs if c <= 20.0]
        if len(clean) >= 20:
            base = sum(clean) / len(clean)
        # 合理性上限：这类 ETF 日均波动不可能 >1%（100bp），
        # 超过说明序列整体坏（511990 东财返的是错乱数据），跳过该标的的极端判定
        if base is not None and base > 1.0:
            base = None
    except Exception as e:
        _logf("极端波动基准 %s 计算失败：%s" % (code, e))
    _BASELINE_CACHE[code] = (now, base)
    return base


def judge_extreme(rows):
    """极端波动判定：|实时估值/前一日净值−1| > 3 × 当年平均日波动。

    返回 (ext_items, ext_hits)：ext_items 全量明细（供展示），ext_hits 已按
    「同一标的同一交易日只报一次」去重。
    ⚠️ 这里只读去重状态不写入 —— 标记在钉钉**发送成功后**才落（run_once），
    发送失败/静默窗口的信号不会被吞，下一轮自动重试。
    """
    by_code = {str(r.get("code")): r for r in rows}
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    ext_items, ext_hits = [], []
    for code in sorted(EXT_VOL_CODES):
        row = by_code.get(code) or {}
        est, nav = _num(row.get("iopv")), _num(row.get("nav"))
        base = _year_baseline(code)
        cur = abs(est / nav - 1) * 100 if (est and nav) else None
        trig = bool(cur is not None and base and base > 0
                    and cur > EXT_VOL_FACTOR * base)
        hit = bool(trig and _EXT_STATE.get(code) != today)
        ext_items.append({
            "code": code,
            "name": row.get("name") or CTRL_WATCH.get(code, {}).get("name", code),
            "est": est, "nav": nav,
            "cur_pct": cur,                       # 当日实时波动%（绝对值）
            "cur_bp": None if cur is None else cur * 100.0,
            "base_pct": base,
            "base_bp": None if base is None else base * 100.0,
            "factor": EXT_VOL_FACTOR,
            "hit": hit,
            "trig": trig,                          # 未去重前的原始触发（展示用）
            "state": (("波动 %.2fbp > 3×基准 %.2fbp" % (cur * 100, base * 100))
                      if trig else ("数据缺失" if cur is None or not base
                                    else "波动 %.2fbp / 阈值 %.2fbp"
                                         % (cur * 100, EXT_VOL_FACTOR * base * 100))),
        })
        if hit:
            ext_hits.append(ext_items[-1])
    return ext_items, ext_hits


# ============ 消息拼装 ============
def build_markdown(items, hits, source="api", ext_items=None):
    """拼钉钉 markdown。表格 = 全部监控标的（命中行带 🔴），下方附赎回到账备忘。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 触发数 = 溢价命中 + 极端波动命中（ext_items 是全量明细，不能直接 len）
    ext_all = ext_items or []
    ext_nd = sum(1 for it in ext_all if it.get("hit"))
    mon_groups = len(items) + len([it for it in ext_all if not any(
        it.get("code") == x.get("code") for x in items)])
    head = ("### %s\n\n> %s · 触发 **%d** / 监控 **%d** 只 · 数据源 %s\n"
            "> 阈值：511880/511990 溢价 ±0.5bp，511360 溢价 ±1bp，511520 溢价 <0bp，"
            "511220 贴水 >40bp；极端波动(7只) > 3×当年平均日波动\n\n"
            "| 信号 | 代码 | 名称 | 现价 | 实时估值 | 实时溢价 | 基金净值 | 净值日期 |\n"
            "|---|---|---|---|---|---|---|---|\n"
            % (TITLE, now, len(hits) + ext_nd, mon_groups, source))

    body = []
    for it in items:
        flag = "🔴" if it["hit"] else "—"
        prem = ("—" if it["prem_bp"] is None
                else "%+.4f%% (%s)" % (it["prem_pct"], _fmt_bp(it["prem_bp"])))
        if it["hit"]:
            prem = "**%s**" % prem
        body.append("| %s | %s | %s | %s | %s | %s | %s | %s |"
                    % (flag, it["code"], it["name"], _fmt(it["price"], 3),
                       _fmt(it["iopv"], 4), prem, _fmt(it["nav"], 4),
                       it["nav_date"] or "—"))

    # 判定明细（把命中原因写清楚，避免只看到一个红点不知道触发了什么）
    detail = "\n\n**判定明细**\n\n"
    for it in items:
        detail += "- %s %s：%s\n" % (it["code"], it["name"], it["state"])
        if it["hit"]:
            detail += "    - 现价 %s ÷ 实时估值 %s → 实时溢价 %s\n" % (
                _fmt(it["price"], 3), _fmt(it["iopv"], 4), _fmt_bp(it["prem_bp"]))
        if it.get("err"):
            detail += "    - 估值备注：%s\n" % str(it["err"])[:120]

    # 🔴 极端波动明细（2026-11-14）：当日已提醒过的只展示不重复推
    if ext_items:
        detail += "\n\n**极端波动（> 3×当年平均日波动，同日只提醒一次）**\n\n"
        for it in ext_items:
            flag = "🔴" if it["trig"] else "—"
            th_bp = (it["base_bp"] * EXT_VOL_FACTOR) if it["base_bp"] is not None else None
            detail += ("- %s **%s %s**：实时估值 %s vs 前一日净值 %s → 当日波动 %s，"
                       "阈值 %s（当年平均 %s × 3）%s\n"
                       % (flag, it["code"], it["name"], _fmt(it["est"], 4),
                          _fmt(it["nav"], 4), _fmt_bp(it["cur_bp"]), _fmt_bp(th_bp),
                          _fmt_bp(it["base_bp"]),
                          "；今日已提醒过，不重复推"
                          if it["trig"] and not it["hit"] else ""))

    note = "\n\n#### 赎回到账备忘\n\n"
    for code, text in NOTES:
        note += "- **%s**：%s\n" % (code, text)

    return head + "\n".join(body) + detail + note


# ============ 单轮 ============
def run_once(dry_run=False, ignore_session=False, always=False, prefer_api=True):
    """执行一轮：取数 → 判定 → 推送。

    返回 dict：{ok, sent, hits, items, reason}
      · 非交易时段 / 无触发 / 钉钉静默窗口 → sent=False，reason 说明原因。
    """
    now = datetime.datetime.now()
    # if not ignore_session and not _is_trading_session(now):
    #     _logf("非交易时段，跳过本轮（%s）" % now.strftime("%Y-%m-%d %H:%M:%S"))
    #     return {"ok": True, "sent": False, "hits": 0, "items": [], "reason": "非交易时段"}

    try:
        rows, source = fetch_rows(prefer_api=prefer_api)
    except Exception as e:
        _logf("取数失败：%s" % e)
        return {"ok": False, "sent": False, "hits": 0, "items": [], "reason": "取数失败: %s" % e}

    items, hits = judge(rows)
    ext_items, ext_hits = judge_extreme(rows)
    msg = build_markdown(items, hits, source="本机API" if source == "api" else "直算",
                         ext_items=ext_items)

    if dry_run:
        print("\n" + "=" * 70)
        print(msg)
        print("=" * 70 + "\n")
        _logf("dry-run：触发 %d 只（溢价 %d / 极端波动 %d），消息已打印未发送"
              % (len(hits) + len(ext_hits), len(hits), len(ext_hits)))
        return {"ok": True, "sent": False, "hits": len(hits) + len(ext_hits),
                "items": items, "reason": "dry-run"}

    if not hits and not ext_hits and not always:
        _logf("无触发（监控 %d 只 + 极端波动 %d 只），不发送"
              % (len(items), len(ext_items)))
        return {"ok": True, "sent": False, "hits": 0, "items": items,
                "reason": "无触发"}

    bot = _notifier()
    if not bot:
        _logf("钉钉通知器不可用，本轮消息未发送（触发 %d 只）" % (len(hits) + len(ext_hits)))
        return {"ok": False, "sent": False, "hits": len(hits) + len(ext_hits),
                "items": items, "reason": "钉钉通知器不可用"}

    try:
        ok = bot.send_alert(msg, TITLE)
    except Exception as e:
        _logf("钉钉发送异常：%s" % e)
        return {"ok": False, "sent": False, "hits": len(hits) + len(ext_hits),
                "items": items, "reason": "发送异常: %s" % e}

    n_total = len(hits) + len(ext_hits)
    desc = "、".join(
        ["%s 溢价%.2fbp" % (h["code"], h["prem_bp"]) for h in hits]
        + ["%s 极端波动%.2fbp" % (h["code"], h["cur_bp"]) for h in ext_hits])
    if ok:
        _logf("已发送钉钉：触发 %d 只（%s）" % (n_total, desc))
        # 🔴 极端波动去重只在真正发送成功后才记「已提醒」（发送失败不吞掉信号）
        for h in ext_hits:
            _EXT_STATE[h["code"]] = now.strftime("%Y-%m-%d")
    else:
        # send_alert 返回 False = 落在 12:30–13:00 静默窗口（或内部异常）
        _logf("钉钉未发送（%s，通常为 12:30-13:00 静默窗口），触发 %d 只"
              % (now.strftime("%H:%M"), n_total))
        # 静默窗口被吞的极端信号：撤回去重标记，下轮重试（与溢价提醒的既约定一致：
        # 不重试；但极端波动是低频重要信号，撤回标记让 13:00 后下一轮补发）
        for h in ext_hits:
            _EXT_STATE.pop(h["code"], None)
    return {"ok": True, "sent": bool(ok), "hits": n_total, "items": items,
            "reason": "已发送" if ok else "静默窗口/未发送"}


# ============ 常驻循环 ============
def run_forever(interval=INTERVAL, **kw):
    """启动即执行一轮，此后每 interval 秒一轮（轮询耗时计入间隔，不快于 interval）。"""
    _logf("策略启动：间隔 %d 秒，监控 %d 只（%s）"
          % (interval, len(CTRL_WATCH), "、".join(CTRL_WATCH)))
    while True:
        t0 = time.time()
        try:
            run_once(**kw)
        except KeyboardInterrupt:
            _logf("策略已停止（Ctrl+C）")
            raise
        except Exception:
            _logf("本轮异常（继续下一轮）：\n%s" % traceback.format_exc())
        time.sleep(max(1.0, interval - (time.time() - t0)))


def start_background(interval=INTERVAL, **kw):
    """给 backend/app.py 用：挂一个 daemon 线程，随看板服务启动一起跑。"""
    import threading
    th = threading.Thread(target=_bg_guard, args=(interval, kw),
                          name="strategy_cash_alert", daemon=True)
    th.start()
    return th


def _bg_guard(interval, kw):
    """线程入口：吞掉一切异常，绝不让策略线程拖垮看板。"""
    try:
        run_forever(interval=interval, **kw)
    except Exception:
        _logf("策略线程退出：\n%s" % traceback.format_exc())


# ============ CLI ============
def main():
    try:
        sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="现金管理 ETF 实时溢价钉钉提醒（每 5 分钟）")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息，不发钉钉")
    ap.add_argument("--interval", type=int, default=INTERVAL, help="轮询间隔秒（默认 300）")
    ap.add_argument("--all-hours", action="store_true", help="非交易时段也执行")
    ap.add_argument("--always", action="store_true", help="不看触发条件，每轮都推送全量表")
    ap.add_argument("--no-http", action="store_true", help="跳过本机 API，直接算（后端未起时用）")
    a = ap.parse_args()

    kw = dict(dry_run=a.dry_run, ignore_session=a.all_hours,
              always=a.always, prefer_api=not a.no_http)
    if a.once:
        r = run_once(**kw)
        print("结果：%s" % r)
        return 0 if r.get("ok") else 1
    try:
        run_forever(interval=a.interval, **kw)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
