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
| 511520 | 政金债ETF富国  | 折价 ≥ 20bp（实时溢价 ≤ −20bp，只报折价）  |
| 511220 | 城投债ETF海富通| 贴水 > 40bp（实时溢价 < −40bp，只报贴水）  |

🔴 2026-11-23 调整
------------------
1. **规则集中**：所有规则口径（阈值、极端波动、套利安全垫、额度说明、赎回到账
   备忘）统一收拢到提醒末尾的「策略规则」板块，不再散落在标题/明细/各小节的抬头。
2. **每日一次**：每个交易日只推送一次，首次出现触发信号时发送，之后各轮即使仍有
   触发也不再重复推送（状态落盘 log/strategy_cash_alert_state.json，进程重启不重复）。
   发送失败或落在 12:30–13:00 静默窗口时不落标记，下轮继续重试。
3. **只发触发数据**：表格与判定明细只列达到触发信号的标的，未触发的不再展示。

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

    # 忽略「每日只推一次」限制，本轮强制推送（调试用）
    python backend/strategy_cash_alert.py --force

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
DAILY_ONCE = True                   # 每个交易日只在首次触发时推送一次
STATE_FILE = os.path.join(LOG_DIR, "strategy_cash_alert_state.json")

# ============ 阈值分档（2026-11-23 重构） ============
# 为什么不再用一套统一 bp 阈值：
#   各标的久期/信用/权益属性差异极大，实测稳健日波动 σ 从 0.06bp（银华日利）
#   到 50.6bp（可转债博时），相差 800 倍。同一个 30bp 在短融上等于 162σ（永不触发），
#   在可转债上等于 0.7σ（每天触发）。所以按 σ 分档，档内用同一个阈值。
#
# 每档阈值 ≈ 该档典型 σ × 20（约 20σ）：
#   20σ 是「一年几次」的量级 —— 常态波动不可能到，真出现就是异常机会。
#   实测校准依据（2026-01-05~11-20，212 个交易日）：
#     档位      典型σ      阈值     = 几σ
#     现金      0.10bp     ±2bp     20σ
#     纯债      1.30bp     ±30bp    23σ   ← 用户指定的 30bp，正好落在此档
#     长久期    4.4~17.8bp ±75bp    17~4σ（取两档折中，兼顾政金债与30年国债）
#     可转债    45bp       ±150bp   3.3σ
#
# 分档依据是「资产属性 + σ」，不是拍脑袋：同一档内的标的 σ 相差 <20%，
# 共用阈值不会出现「一只天天报、一只永不报」。
VOL_TIERS = {
    "cash":    {"label": "现金/短融",  "th_bp": 2.0,   "sigma_bp": 0.1},
    "pure":    {"label": "纯债",      "th_bp": 30.0,  "sigma_bp": 1.3},
    "longdur": {"label": "政金/长久期", "th_bp": 75.0,  "sigma_bp": 8.0},
    "convert": {"label": "可转债",    "th_bp": 150.0, "sigma_bp": 45.0},
}

# 监控标的：code -> 触发规则
#   mode = "both"  : |溢价| 超阈值（双向都报）
#   mode = "below" : 溢价 ≤ th 才报（th 为负：折价/贴水单边）
#   2026-11-23：从 5 只扩到全部 18 只（数据库 fund_profile 的 B+C 两个市场全覆盖），
#   解决「做市信用债/科创债 9 只此前完全没被监控」的问题。
#   档位见 VOL_TIERS；th_bp 允许单标的覆盖（None = 用档位默认值）。
CTRL_WATCH = {
    # ---- 现金/短融：σ≈0.1bp，阈值 ±2bp ----
    "511880": {"name": "银华日利ETF",    "tier": "cash",   "mode": "both"},
    "511990": {"name": "华宝添益ETF",    "tier": "cash",   "mode": "both"},
    "511360": {"name": "短融ETF海富通",  "tier": "cash",   "mode": "both", "th_bp": 1.5},

    # ---- 纯债主力档：σ≈1.3bp，统一 ±30bp（做市信用债 + 科创债 + 城投）----
    # 2026-11-23：做市公司债 4 只 + 科创债 5 只 + 城投 1 只，全部纳入并统一 30bp。
    "511070": {"name": "公司债ETF南方",  "tier": "pure", "mode": "both"},
    "511110": {"name": "公司债ETF易方达", "tier": "pure", "mode": "both"},
    "511190": {"name": "信用债ETF海富通", "tier": "pure", "mode": "both"},
    "511200": {"name": "信用债ETF华夏",  "tier": "pure", "mode": "both"},
    "551030": {"name": "科创债ETF鹏华",  "tier": "pure", "mode": "both"},
    "551500": {"name": "科创债ETF易方达", "tier": "pure", "mode": "both"},
    "551550": {"name": "科创债ETF华夏",  "tier": "pure", "mode": "both"},
    "551900": {"name": "科创债ETF招商",  "tier": "pure", "mode": "both"},
    "159600": {"name": "科创债ETF嘉实",  "tier": "pure", "mode": "both"},
    "511220": {"name": "城投债ETF海富通", "tier": "pure", "mode": "both"},

    # ---- 政金债/长久期：σ 4.4~17.8bp，阈值 ±75bp ----
    "511520": {"name": "政金债ETF富国",  "tier": "longdur", "mode": "both"},
    "511090": {"name": "30年国债ETF鹏扬", "tier": "longdur", "mode": "both"},
    "511130": {"name": "30年国债ETF博时", "tier": "longdur", "mode": "both"},

    # ---- 可转债：σ≈45bp，阈值 ±150bp（权益属性，独立一档）----
    "511180": {"name": "可转债ETF海富通", "tier": "convert", "mode": "both"},
    "511380": {"name": "可转债ETF博时",  "tier": "convert", "mode": "both"},
}


def _th_of(cfg):
    """取有效阈值：单标的覆盖优先，否则用档位默认。"""
    th = cfg.get("th_bp")
    if th is not None:
        return float(th)
    tier = VOL_TIERS.get(cfg.get("tier") or "", None)
    return float(tier["th_bp"]) if tier else 30.0


def _tier_label(cfg):
    tier = VOL_TIERS.get(cfg.get("tier") or "", None)
    return tier["label"] if tier else "其他"

# 🔴 2026-11-21 新增：三只现金管理工具的「折溢价套利可行性」判定
#   由 backend/cash_arb_advisor.py 实现：考虑节假日/周几导致的资金占用天数、
#   与 GC001 逆回购收益对比、溢价 1.5× 安全垫、到账时间与申赎额度。
ARB_CODES = ("511880", "511990", "511360")
try:
    import cash_arb_advisor as _arb
except Exception:                  # 套利模块故障不阻断既有提醒
    _arb = None

# 🔴 极端波动提醒（2026-11-23 扩容：覆盖债券 B + 现金 C 两个市场全部 18 只）
#   当日实时波动 = |实时估值(iopv) ÷ 前一日净值(nav) − 1|
#   阈值 = 当年日净值波动的 **第 99 百分位**（p99）
#   触发 = 当日波动 > p99 阈值
#
#   为什么用本地 unified_fund_history 而不是东财 lsjz：
#     1. 本地已有 18 只 × 213 天净值（2026-01-05 起，写作时最新 11-20），
#        东财要逐只翻 11 页 × 18 只 = 198 次请求，每轮 5 分钟跑一遍不现实；
#     2. 本地是同源数据（东财净值），口径一致，少一层网络失败点。
#   阈值口径（2026-11-23 二次修正）：用**历史分位数**而不是 k×σ。
#     实测发现这批净值是厚尾分布（单日正态峰+跳跃），MAD 算出的 σ 严重低估尾部：
#       511360 σ=0.19bp，3.5σ=0.65bp 只落在第 75 百分位 → 一年要报几十次；
#       511070 σ=1.39bp，3.5σ=4.88bp 落在第 91 百分位 → 一年报十几次。
#     都不是「异常」。改为直接用 p99 分位当阈值：
#       p99 = 该标的历史第 99 百分位日波动 ⇒ 超过它平均一年只发生 2~3 次，
#       这才是「极端」的字面含义，且对厚尾天然免疫，不需要假设分布形状。
#     今日实测：p99 口径下 16 只可算标的中只触发 2 只（159600/551900），符合预期。
EXT_VOL_PCTL = 0.99           # 用历史第 99 百分位当触发阈值
EXT_VOL_MIN_SAMPLES = 60      # 少于 60 个交易日的净值不算基准（新股/新债）
EXT_VOL_CODES = None          # None = 自动取 CTRL_WATCH 全部标的（18 只）
_EXT_STATE = {}               # {code: 当日已提醒标记 'YYYY-MM-DD'}（同日去重）
_BASELINE_CACHE = {}          # {code: (date_str, p99_pct, p99_bp)}（按自然日缓存）

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


# ============ 每日一次状态（落盘，进程重启不重复推） ============
def _read_sent_date():
    """读已推送日期；文件不存在/损坏返回 None。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("sent_date")
    except Exception:
        return None


def _write_sent_date(today):
    """只在钉钉发送成功后调用；写失败仅记日志，不回滚已发送的事实。"""
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sent_date": today}, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        _logf("每日推送状态落盘失败（不影响本次发送）：%s" % e)


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
        th = _th_of(cfg)                                # 分档阈值（单标的可覆盖）
        mode = cfg["mode"]

        if prem_bp is None:
            state, hit = "数据缺失", False
        elif mode == "below":
            hit = prem_bp <= -th
            state = ("折价/贴水 %+.2fbp ≤ -%.0fbp" % (prem_bp, th) if hit
                     else "阈值内(-%.0fbp)" % th)
        else:
            hit = (prem_bp >= th) or (prem_bp <= -th)
            if hit:
                state = ("溢价 %+.2fbp ≥ +%.0fbp" % (prem_bp, th) if prem_bp > 0
                         else "折价 %+.2fbp ≤ -%.0fbp" % (prem_bp, th))
            else:
                state = "阈值内(±%.0fbp)" % th

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
            "tier": cfg.get("tier") or "",
            "tier_label": _tier_label(cfg),
            "err": row.get("err"),
        }
        items.append(item)
        if hit:
            hits.append(item)

    _mark_crowding(hits)
    return items, hits


# ---- 同向拥挤检测：解决「阈值一碰、同档 8 只一起报」的刷屏问题 ----
# 2026-11-23 实测：8 只沪市纯债同日折价全部落在 -27.8±1.9bp，横截面 std 仅 1.45 倍
# 于单只日波动 σ。这不是 8 个独立机会，而是同一个系统性偏移（估值基准整体偏高/
# 资金面整体紧张）打在 8 只标的上。逐条列出等于把 1 个信号说 8 遍。
# 处理：同档内同向触发 ≥ CROWD_MIN 只，且横截面离散度（std）小于该档 σ × 1.5，
# 判为「系统性偏移」，只保留 |偏离| 最大的一只做代表，其余标记 crowded 不计入推送。
CROWD_MIN = 3           # 同档同向至少 3 只才算拥挤
CROWD_SPREAD_K = 1.5    # 横截面 std < 档位σ × 该倍数 → 视为同源


def _mark_crowding(hits):
    """就地为 hits 打上 crowded / crowd_group / crowd_note 标记（不发散）。"""
    from collections import defaultdict
    groups = defaultdict(list)
    for h in hits:
        side = "prem" if (h["prem_bp"] or 0) > 0 else "disc"
        groups[(h["tier"], side)].append(h)

    for (tier, side), arr in groups.items():
        if len(arr) < CROWD_MIN:
            continue
        vals = [abs(h["prem_bp"] or 0) for h in arr]
        n = len(vals)
        mean = sum(vals) / n
        std = (sum((v - mean) ** 2 for v in vals) / n) ** 0.5
        sigma_bp = (VOL_TIERS.get(tier) or {}).get("sigma_bp") or 0
        # std 远小于单只日波动 ⇒ 8 只几乎是同一个数，说明是共同偏移不是个性机会
        systematic = sigma_bp > 0 and std <= max(sigma_bp * CROWD_SPREAD_K, 1.0)
        rep = max(arr, key=lambda h: abs(h["prem_bp"] or 0))
        for h in arr:
            h["crowd_n"] = n
            h["crowd_std_bp"] = round(std, 2)
            h["crowd_systematic"] = systematic
            h["crowd_side"] = "溢价" if side == "prem" else "折价"
            if systematic and h is not rep:
                h["crowded"] = True
                h["crowd_note"] = ("同档 %d 只同向%s（离散度仅 %.2fbp，属系统性偏移，"
                                   "非个体机会）" % (n, h["crowd_side"], std))
            else:
                h["crowded"] = False
                if systematic:
                    h["crowd_note"] = ("同档 %d 只同向%s，离散度 %.2fbp → 代表标的"
                                       % (n, h["crowd_side"], std))


# ============ 极端波动（2026-11-23 改为本地净值基准） ============
def _fetch_nav_year(code, max_pages=14):
    """东财 lsjz 翻页抓「当年」净值序列（**已降级为兜底**，主路径走本地 DB）。

    ⚠️ lsjz 实测每页最多 20 行，全年 ~220 行需翻 ~11 页 × 18 只 = 198 次请求，
    每 5 分钟一轮完全不现实 ⇒ 仅在本地库无该标的历史时才调用。
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


def _nav_series_local(code, year=None):
    """从本地 unified_fund_history 读当年净值序列 [(date, nav)] 升序。

    与东财 lsjz 同源（本库净值就是东财数据），但零网络开销、抗失败。
    取不到时返回 []，由调用方决定是否回退网络。
    """
    year = year or datetime.datetime.now().year
    try:
        import sqlite3
        import db as _db
        conn = sqlite3.connect(_db.DB_PATH, timeout=10)
        try:
            rows = conn.execute(
                "SELECT date, nav FROM unified_fund_history "
                "WHERE fund_code=? AND nav>0 AND substr(date,1,4)=? "
                "ORDER BY date", (code, str(year))).fetchall()
        finally:
            conn.close()
        return [(str(d), float(v)) for d, v in rows if d and v]
    except Exception:
        return []


def _year_baseline(code):
    """当年日净值波动的 **p99 分位阈值**（百分数，如 0.15 = 15bp）。

    为什么用分位数而不是 k×σ：
      实测这批净值序列厚尾，MAD 估的 σ 把尾部压得太低，3.5σ 只到第 75~94 百分位，
      等于天天/周周触发；p99 直接锁在「一年约 2~3 次」，对分布形状无需任何假设。
    返回 None = 样本不足（<60）或序列不可信，该标的本轮跳过极端判定。
    按自然日缓存：净值序列日内不变，没必要每 5 分钟重算。
    """
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    cached = _BASELINE_CACHE.get(code)
    if cached and cached[0] == today:
        return cached[1]

    base = None
    try:
        ys = _nav_series_local(code)
        if len(ys) < EXT_VOL_MIN_SAMPLES + 1:
            ys = _fetch_nav_year(code)          # 本地没有 → 兜底走网络
        chgs = [abs(ys[i][1] / ys[i - 1][1] - 1) * 100
                for i in range(1, len(ys)) if ys[i - 1][1]]
        chgs = [c for c in chgs if c <= 20.0]   # 剔除 >20% 的尺度错乱
        if len(chgs) >= EXT_VOL_MIN_SAMPLES:
            chgs.sort()
            n = len(chgs)
            idx = min(int(n * EXT_VOL_PCTL), n - 1)
            p = chgs[idx]
            # 合理性上限：债券/货币 ETF 日波动 p99 不可能 >5%（500bp）；
            # 超过说明整个序列坏（净值尺度混串），不能用它做阈值
            base = p if 0 < p <= 5.0 else None
    except Exception as e:
        _logf("极端波动基准 %s 计算失败：%s" % (code, e))

    _BASELINE_CACHE[code] = (today, base)
    return base


def _ext_codes():
    """极端波动监控标的：默认取 CTRL_WATCH 全部（债券 B + 现金 C 共 18 只）。"""
    if EXT_VOL_CODES:
        return sorted(EXT_VOL_CODES)
    return sorted(CTRL_WATCH)


def judge_extreme(rows):
    """极端波动判定：|实时估值/前一日净值−1| > 当年日波动的 p99 分位阈值。

    返回 (ext_items, ext_hits)：ext_items 全量明细（供展示），ext_hits 已按
    「同一标的同一交易日只报一次」去重。
    ⚠️ 这里只读去重状态不写入 —— 标记在钉钉**发送成功后**才落（run_once），
    发送失败/静默窗口的信号不会被吞，下一轮自动重试。
    """
    by_code = {str(r.get("code")): r for r in rows}
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    ext_items, ext_hits = [], []
    for code in _ext_codes():
        row = by_code.get(code) or {}
        est, nav = _num(row.get("iopv")), _num(row.get("nav"))
        base = _year_baseline(code)
        cur = abs(est / nav - 1) * 100 if (est and nav) else None
        trig = bool(cur is not None and base and base > 0 and cur > base)
        hit = bool(trig and _EXT_STATE.get(code) != today)
        ext_items.append({
            "code": code,
            "name": row.get("name") or CTRL_WATCH.get(code, {}).get("name", code),
            "est": est, "nav": nav,
            "cur_pct": cur,                       # 当日实时波动%（绝对值）
            "cur_bp": None if cur is None else cur * 100.0,
            "base_pct": base,
            "base_bp": None if base is None else base * 100.0,
            "ratio": (cur / base) if (cur is not None and base) else None,
            "hit": hit,
            "trig": trig,                          # 未去重前的原始触发（展示用）
            "state": (("波动 %.2fbp > p99 %.2fbp" % (cur * 100, base * 100))
                      if trig else ("数据缺失（净值样本 < %d 日）" % EXT_VOL_MIN_SAMPLES
                                    if cur is None or not base
                                    else "波动 %.2fbp / p99 %.2fbp"
                                         % (cur * 100, base * 100))),
        })
        if hit:
            ext_hits.append(ext_items[-1])
    return ext_items, ext_hits


# ============ 折溢价套利可行性（2026-11-21 新增） ============
def judge_arb(rows):
    """用 cash_arb_advisor 分析三只现金管理工具的折溢价套利机会。

    返回 (res, arb_hits)：
      res      : 完整分析结果（供 markdown 渲染，含占用天数/到账时间/额度）
      arb_hits : 有可行套利机会的标的 [{code,name,kind,...}]，用于并入触发集合
    任一异常都吞掉并返回空，绝不影响既有提醒。
    """
    if _arb is None:
        return None, []
    try:
        res = _arb.analyze(rows)
    except Exception as e:
        _logf("套利分析失败（不影响既有提醒）：%s" % e)
        return None, []
    hits = []
    for code, it in res.get("items", {}).items():
        kinds = []
        if it["discount"]["feasible"]:
            kinds.append("折价%.2fbp→赎回" % it["discount"]["discount_bp"])
        if it["premium"]["feasible"]:
            kinds.append("溢价%.2fbp→申购" % it["premium"]["premium_bp"])
        if kinds:
            hits.append({"code": code, "name": it["name"], "kind": "/".join(kinds)})
    return res, hits


# ============ 消息拼装 ============
def build_rules_markdown(arb_res=None):
    """把所有「规则口径」收拢到唯一板块：阈值 / 极端波动 / 套利 / 到账备忘 / 额度。"""
    lines = ["\n\n---\n\n#### 📌 策略规则（每交易日仅首次触发时推送）\n\n"]

    # 阈值按档位归纳，而不是把 18 只逐个列出来（那样没人看得完）
    by_tier = {}
    for code, cfg in CTRL_WATCH.items():
        by_tier.setdefault(cfg.get("tier") or "other", []).append(code)
    lines.append("- **折溢价阈值（按波动率分档，档内统一）**：\n")
    for tier_key, tier in VOL_TIERS.items():
        codes = by_tier.get(tier_key) or []
        if not codes:
            continue
        lines.append("    - **%s**（σ≈%sbp，%d 只）：±%.0fbp\n"
                     % (tier["label"], tier["sigma_bp"], len(codes), tier["th_bp"]))
    lines.append("    - 阈值 ≈ 该档典型 σ × 20，使各档触发频率都在「一年几次」量级；"
                 "久期不同不共用同一个 bp 数\n")

    lines.append("- **极端波动**（%d 只，覆盖债券+现金全部标的）：当日实时波动 > 该标的当年"
                 "日波动 **p99 分位**触发，同一标的同一交易日只提醒一次\n" % len(_ext_codes()))
    lines.append("- **同向拥挤**：同档内 ≥%d 只同向触发且横截面离散度 < 档位σ×%.1f 时，"
                 "判为系统性偏移，只推代表标的，避免同一个信号报 N 遍\n"
                 % (CROWD_MIN, CROWD_SPREAD_K))

    if _arb is not None:
        try:
            lines.append(_arb.format_rules_markdown(arb_res))
        except Exception as e:
            lines.append("- **套利规则**：渲染失败 %s\n" % e)

    lines.append("- **赎回到账备忘**：\n")
    for code, text in NOTES:
        lines.append("    - **%s**：%s\n" % (code, text.replace("\n", "\n      ")))
    lines.append("\n> 本策略每交易日只在首次出现触发信号时推送一次，"
                 "后续各轮即使仍有信号也不再重复发送。\n")
    return "".join(lines)


def build_markdown(items, hits, source="api", ext_items=None, arb_res=None, arb_hits=None):
    """拼钉钉 markdown。**表格与明细只列触发标的**；规则统一放末尾板块。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # ext_items 是全量明细，只有 hit=True 的才是本轮新增触发（同日去重的极端信号）
    ext_hits = [it for it in (ext_items or []) if it.get("hit")]
    arb_hits = arb_hits or []

    # 触发原因索引：一只标的可同时命中溢价/极端波动/套利
    reasons = {}
    crowd_hit = []          # 被判定为「同向拥挤」而从推送中折叠的标的
    for it in hits:
        if it.get("crowded"):
            crowd_hit.append(it)
            continue
        reasons.setdefault(it["code"], []).append(
            "溢价" if (it.get("prem_bp") or 0) > 0 else "折价")
    for it in ext_hits:
        reasons.setdefault(it["code"], []).append("极端波动")
    for it in arb_hits:
        reasons.setdefault(it["code"], []).append("套利")

    trig_codes = set(reasons)
    n_total = len([h for h in hits if not h.get("crowded")]) + len(ext_hits) + len(arb_hits)
    head = ("### %s\n\n> %s · 触发 **%d** 项（折溢价 %d / 极端波动 %d / 套利 %d）"
            " · 数据源 %s · 每日仅首次触发推送\n\n"
            "| 信号 | 档位 | 代码 | 名称 | 现价 | 实时估值 | 实时溢价 | 基金净值 | 净值日期 | 操作方式 | 最小申赎 |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            % (TITLE, now, n_total,
               len([h for h in hits if not h.get("crowded")]),
               len(ext_hits), len(arb_hits), source))

    # 只保留触发行；极端波动若命中不在折溢价清单里的标的，补一行展示
    show = [it for it in items if it["code"] in trig_codes]
    shown_codes = {it["code"] for it in show}
    for it in ext_hits:
        if it["code"] not in shown_codes:
            show.append({
                "code": it["code"], "name": it["name"], "price": None,
                "iopv": it["est"], "prem_pct": None, "prem_bp": None,
                "nav": it["nav"], "nav_date": None, "hit": True,
                "state": it["state"], "err": None,
                "tier_label": _tier_label(CTRL_WATCH.get(it["code"], {})),
            })
            shown_codes.add(it["code"])

    arb_items = {}
    if arb_res and _arb is not None:
        arb_items = {c: v for c, v in (arb_res.get("items") or {}).items()}

    body = []
    for it in show:
        sig = "🔴 " + "/".join(reasons.get(it["code"], ["触发"]))
        prem = ("—" if it["prem_bp"] is None
                else "%+.4f%% (%s)" % (it["prem_pct"], _fmt_bp(it["prem_bp"])))
        # 操作方式 / 最小申赎（仅三只现金工具有申赎套利规则，其余标的显示 —）
        ai = arb_items.get(it["code"])
        if ai:
            action = ai.get("action", "观望")
            action_cell = ("**%s**" % action) if action in ("赎回", "申购") else action
            ma = ai.get("min_apply") or {}
            min_cell = "%s份/%s" % (ma.get("shares"), ma.get("amount"))
        else:
            action_cell, min_cell = "—", "—"
        body.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                    % (sig, it.get("tier_label") or "—", it["code"], it["name"],
                       _fmt(it["price"], 3),
                       _fmt(it["iopv"], 4), prem, _fmt(it["nav"], 4),
                       it["nav_date"] or "—", action_cell, min_cell))

    # 判定明细：只写触发标的，并把极端波动 / 套利原因并列清楚
    detail = "\n\n**判定明细（仅触发项）**\n\n" if show else "\n\n**判定明细**\n\n无\n"
    for it in show:
        code = it["code"]
        detail += "- **%s %s**：%s\n" % (code, it["name"], it["state"])
        if it["prem_bp"] is not None:
            detail += "    - 现价 %s ÷ 实时估值 %s → 实时溢价 %s\n" % (
                _fmt(it["price"], 3), _fmt(it["iopv"], 4), _fmt_bp(it["prem_bp"]))
        for e in ext_hits:
            if e["code"] == code:
                ratio = ("（%.1f× p99）" % e["ratio"]) if e.get("ratio") else ""
                detail += ("    - 极端波动：实时估值 %s vs 前一日净值 %s → 当日 %s，"
                           "该标的当年日波动 p99 = %s%s\n"
                           % (_fmt(e["est"], 4), _fmt(e["nav"], 4),
                              _fmt_bp(e["cur_bp"]), _fmt_bp(e["base_bp"]), ratio))
        for a in arb_hits:
            if a["code"] == code:
                detail += "    - 套利机会：%s\n" % a["kind"]
        if it.get("crowd_note"):
            detail += "    - 拥挤判定：%s\n" % it["crowd_note"]
        if it.get("err"):
            detail += "    - 估值备注：%s\n" % str(it["err"])[:120]

    # 被折叠的拥挤标的：一句话汇总，让用户知道「还有 7 只一样但没逐条列」
    if crowd_hit:
        by_side = {}
        for it in crowd_hit:
            by_side.setdefault(it.get("crowd_side") or "折价", []).append(it)
        detail += "\n**已折叠的同向标的（系统性偏移，非独立机会）**\n\n"
        for side, arr in by_side.items():
            codes = "、".join("%s %s(%s)" % (h["code"], h["name"],
                                            _fmt_bp(h["prem_bp"])) for h in arr)
            detail += "- 同向%s %d 只：%s\n" % (side, len(arr), codes)
        detail += ("> 这些标的与代表标的的偏离几乎相同（离散度 < 档位σ×%.1f），"
                   "本质是同一个估值/资金面偏移打在多个标的上，逐条列出等于把"
                   " 1 个信号说 %d 遍，故只保留偏离最大的一只。\n" % (CROWD_SPREAD_K, len(crowd_hit) + 1))

    # 折溢价套利机会明细（仅列出已触发的可行机会；规则已收拢到末尾板块）
    arb_block = ""
    if arb_hits and arb_res and _arb is not None:
        try:
            arb_block = "\n\n" + _arb.format_arb_markdown(arb_res)
        except Exception as e:
            arb_block = "\n\n> 套利明细渲染失败：%s\n" % e

    return head + "\n".join(body) + detail + arb_block + build_rules_markdown(arb_res)


# ============ 单轮 ============
def run_once(dry_run=False, ignore_session=False, prefer_api=True, force=False):
    """执行一轮：取数 → 判定 → 推送（每日仅首次触发推送一次，force 可绕过）。

    返回 dict：{ok, sent, hits, items, reason}
      · 无触发 / 今日已推送 / 钉钉静默窗口 → sent=False，reason 说明原因。
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
    arb_res, arb_hits = judge_arb(rows)
    msg = build_markdown(items, hits, source="本机API" if source == "api" else "直算",
                         ext_items=ext_items, arb_res=arb_res, arb_hits=arb_hits)

    n_total = len([h for h in hits if not h.get("crowded")]) + len(ext_hits) + len(arb_hits)
    n_crowd = len([h for h in hits if h.get("crowded")])
    if dry_run:
        print("\n" + "=" * 70)
        print(msg)
        print("=" * 70 + "\n")
        _logf("dry-run：触发 %d 只（折溢价 %d / 极端波动 %d / 套利 %d）%s，消息已打印未发送"
              % (n_total, len(hits) - n_crowd, len(ext_hits), len(arb_hits),
                 ("，另折叠同向拥挤 %d 只" % n_crowd) if n_crowd else ""))
        return {"ok": True, "sent": False, "hits": n_total,
                "crowded": n_crowd, "items": items, "reason": "dry-run"}

    if not n_total:
        _logf("无触发（监控 %d 只 + 极端波动 %d 只 + 套利 %d 只）%s，不发送"
              % (len(items), len(ext_items), len(arb_hits),
                 ("，折叠同向 %d 只" % n_crowd) if n_crowd else ""))
        return {"ok": True, "sent": False, "hits": 0, "items": items,
                "reason": "无触发"}

    # 🔴 每日一次：首次有触发时推送；状态落盘，进程重启也不会重复打扰
    if DAILY_ONCE and not force:
        today = now.strftime("%Y-%m-%d")
        if _read_sent_date() == today:
            _logf("今日 %s 已推送过，跳过本轮（仍有触发 %d 项）" % (today, n_total))
            return {"ok": True, "sent": False, "hits": n_total, "items": items,
                    "reason": "今日已推送"}

    bot = _notifier()
    if not bot:
        _logf("钉钉通知器不可用，本轮消息未发送（触发 %d 只）" % n_total)
        return {"ok": False, "sent": False, "hits": n_total,
                "items": items, "reason": "钉钉通知器不可用"}

    try:
        ok = bot.send_alert(msg, TITLE)
    except Exception as e:
        _logf("钉钉发送异常：%s" % e)
        return {"ok": False, "sent": False, "hits": n_total,
                "items": items, "reason": "发送异常: %s" % e}

    desc = "、".join(
        ["%s 折溢价%s" % (h["code"], _fmt_bp(h["prem_bp"])) for h in hits]
        + ["%s 极端波动%s" % (h["code"], _fmt_bp(h["cur_bp"])) for h in ext_hits]
        + ["%s 套利(%s)" % (h["code"], h["kind"]) for h in arb_hits])
    if ok:
        _logf("已发送钉钉：触发 %d 项（%s）" % (n_total, desc))
        # 🔴 每日一次：只在钉钉真正发送成功后落标记（失败/静默窗口下轮继续重试）
        if DAILY_ONCE and not force:
            _write_sent_date(now.strftime("%Y-%m-%d"))
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
    ap.add_argument("--force", action="store_true", help="忽略每日一次限制，强制本轮推送")
    ap.add_argument("--no-http", action="store_true", help="跳过本机 API，直接算（后端未起时用）")
    a = ap.parse_args()

    kw = dict(dry_run=a.dry_run, ignore_session=a.all_hours,
              force=a.force, prefer_api=not a.no_http)
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
