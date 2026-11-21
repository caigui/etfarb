# -*- coding: utf-8 -*-
"""511360 短融ETF海富通 —— 独立估值方法（方法二：PCF 逐券篮子）。

2026-11-14 起替换 bond_etf_valuation 里 511360 原有的
`nav(D-1) + 固定票息 0.003 + 0.07 × 国债指数涨幅` 粗估口径。

════════════════════ 为什么单独一个方法 ════════════════════
本文件对外只有**一个估值入口** `estimate_511360_nav(...)`，所有算法参数集中在
文件头 ALGO_* / DEFAULT_* 常量里。以后要换算法（比如接中债估值、改久期源、
改资产结构权重），只需替换这一个方法或改这几个常量，不用动看板/历史页/
取数链路的任何其他文件。bond_etf_valuation 只负责「调用 + 失败降级」。

════════════════════ 算法（移植自 fund_511360_realtime.py 方法二）════════════════════
1) 基数：海富通官网每日申赎清单(PCF) 的「替代金额 ÷ (数量×10)」= 每只成份券
   **全价**（含应计利息）。篮子恒等式实测：
       (Σ替代金额 + 预估现金) / 最小申赎单位 = 官方净值   （偏差 0.00bp）
2) 逐券重估：
       px_base_i = 替代金额_i / (数量_i × 10)
       ΔP_i%     = −修正久期_i × Δy_i(bp) / 100
       px_est_i  = px_base_i × (1 + carry_bond_rate + ΔP_i% / 100)
   · Δy_i：本券成交 bp × DY_WEIGHT（有效样本 |bp| ≤ 10）；无成交的券退化为
     「成交 bp 中位数 × DY_WEIGHT」的组合基准（短融报价整数化、中位数抗噪）。
   · carry_bond_rate = 债券组合到期收益率 / 100 / 365 × 计息天数
     （成交收益率按篮子权重加权，全缺时兜底 DEFAULT_YTM）。
3) 经济结构（2026 三季报）：债券 82.80% / 买入返售 17.16% / 其他 0.04%。
   PCF 篮子是申赎口径（≈99.9% 债券），只取它的**相对变动**并锚定最新官方净值：
       NAV_basket = nav0 × (Σ数量×10×px_est + 预估现金) / (Σ替代金额 + 预估现金)
       NAV_repo   = nav0 × (1 + FR007/100/365 × 计息天数)
       NAV_est    = 0.8280 × NAV_basket + 0.1716 × NAV_repo + 0.0004 × nav0
   回购部分只生息、不吃短端利率的价格效应。
   🔴 锚定最新官方净值（而不是直接拿 PCF 的绝对篮子价当净值）：官网周末/未披露
      时回落到上一交易日 PCF，绝对价会落后一天；锚定后只取相对变动，不重复计息。
4) 日期对齐：现券行情日 ≤ 最新官方净值日（盘后/周末）⇒ 价格效应归零，
   但仍按逐券票息 + FR007 回购息计算，输出**计算值**，不冻结官方净值、不做拟合。

════════════════════ 数据源 ════════════════════
  · PCF 清单   : 海富通官网 XML（按交易日下载并落盘 data/bond_511360/）
  · 现券成交   : 中国货币网 CbtPri（净价 / 收益率 / 涨跌 bp / 剩余期限）
  · 回购利率   : 中国货币网 frr-chrt.csv（FR007，含历史序列供历史页按日取用）
  · 官方净值   : 由 bond_etf_valuation 的净值历史缓存传入（天天基金 f10/lsjz）
"""
import json
import logging
import os
import re
import ssl
import statistics
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

logger = logging.getLogger("bond_511360_valuation")

# ── 算法标识：历史记录页会原样展示这三行，换算法时只改这里 ──────────────
ALGO_ID = "511360_pcf_basket_v1"
ALGO_NAME = "PCF逐券篮子·三季报结构+FR007"
ALGO_VERSION = "2026-11-14"
ALGO_ENTRY = "core/bond_511360_valuation.estimate_511360_nav"

# ── 可调参数（换算法时只需动这一段）──────────────────────────────────
BOND_W = 0.8280            # 三季报：固定收益投资（债券篮子）占比
REPO_W = 0.1716            # 三季报：买入返售金融资产（逆回购）占比
DEFAULT_YTM = 1.45         # 债券组合到期收益率兜底(%)
DEFAULT_REPO_YTM = 1.42    # 7 天回购定盘利率 FR007 兜底(%)
DEFAULT_DUR = 0.45         # 无剩余期限字段时的单券修正久期兜底(年)
DY_WEIGHT = 0.9            # 成交行情 Δy 的采用权重（0=只用 carry）
DY_LIMIT_BP = 10.0         # |成交 bp| 超过此值视为噪声，不参与中位数/逐券
ACC_YEAR_DAYS = 365        # ACT/365
PCF_TTL = 1800             # 当日 PCF 在线抓取缓存 30 分钟
SPOT_TTL = 60              # 现券成交缓存 60 秒
FRR_TTL = 1800             # FR007 序列缓存 30 分钟

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "data", "bond_511360")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HFT_PCF_URL = ("https://www.hftfund.com/upload/applications/funds-struts/"
               "pcf{code}/{code}{ds}.xml")
CM_SPOT_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-md-bond/CbtPri"
CM_FRR_CSV = ("https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/"
              "currency/frr-chrt.csv")

_UA_HEAD = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
_UNVERIFIED = None
_pcf_lock = threading.Lock()
_pcf_cache = {"t": 0.0, "v": None}
_spot_cache = {"t": 0.0, "v": None}
_frr_cache = {"t": 0.0, "v": None}
_state = {"failed": False}


def _log_state(msg, ok=False):
    """只在状态切换时打日志：看板 30 秒一刷，不能每次降级刷一条 WARNING。"""
    if ok:
        if _state["failed"]:
            logger.info("[511360] %s", msg)
            _state["failed"] = False
        return
    if not _state["failed"]:
        logger.warning("[511360] %s", msg)
        _state["failed"] = True
    else:
        logger.debug("[511360] %s", msg)


# ════════════════════════════ HTTP（本机证书链不全时自动降级）════════════════════════════
def _unverified_ctx():
    global _UNVERIFIED
    if _UNVERIFIED is None:
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        _UNVERIFIED = c
    return _UNVERIFIED


def http_open(url, referer="https://www.hftfund.com/", data=None, ctype=None,
              timeout=20):
    headers = dict(_UA_HEAD)
    headers["Referer"] = referer
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        if "CERTIFICATE" not in str(e).upper() and "SSL" not in str(e).upper():
            raise
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_unverified_ctx()) as r:
            return r.read()


def _get_text(url, referer, enc="utf-8", timeout=20):
    return http_open(url, referer=referer, timeout=timeout).decode(enc, "ignore")


def _post_json(url, payload, referer, timeout=30):
    raw = http_open(url, referer=referer,
                    data=json.dumps(payload).encode("utf-8"),
                    ctype="application/json", timeout=timeout)
    return json.loads(raw.decode("utf-8", "ignore"))


# ════════════════════════════ PCF（海富通官网 XML）════════════════════════════
def _tag(block, tag, default=None):
    m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), block, re.S)
    return m.group(1).strip() if m else default


def parse_pcf_xml(text):
    """解析 ETFDefinition XML → 统一结构（与原型缓存的 JSON 同字段）。"""
    unit = float(_tag(text, "CreationRedemptionUnit", "2000") or 2000)
    nav_per_cu = float(_tag(text, "NAVperCU", "0") or 0)
    nav = float(_tag(text, "NAV", "0") or (nav_per_cu / unit if unit else 0))
    fmt = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else None
    bonds = []
    for blk in re.findall(r"<Component>(.*?)</Component>", text, re.S):
        code = _tag(blk, "InstrumentID", "")
        name = _tag(blk, "InstrumentName", "")
        try:
            qty = float(_tag(blk, "Quantity", "0") or 0)
            amt = float(_tag(blk, "SubstitutionCashAmount", "0") or 0)
        except ValueError:
            continue
        if code and name:
            bonds.append({"code": code, "name": name, "qty": qty, "amount": amt})
    if not bonds:
        raise RuntimeError("PCF XML 未解析到成份券")
    return {
        "code": _tag(text, "FundInstrumentID", ""),
        "name": _tag(text, "FundName", ""),
        "date": fmt(_tag(text, "TradingDay", "") or "")
                or datetime.now().strftime("%Y-%m-%d"),
        "pre_date": fmt(_tag(text, "PreTradingDay", "") or ""),
        "nav": nav, "nav_per_cu": nav_per_cu or nav * unit,
        "est_cash": float(_tag(text, "EstimatedCashComponent", "0") or 0),
        "pre_cash": float(_tag(text, "PreCashComponent", "0") or 0),
        "unit": unit, "bonds": bonds, "source": "海富通官网XML",
    }


def _pcf_file(code, d):
    return os.path.join(DATA_DIR, "pcf_%s_%s.json" % (code, d.replace("-", "")))


def _save_pcf(pcf, code="511360"):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_pcf_file(code, pcf["date"]), "w", encoding="utf-8") as f:
            json.dump(pcf, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.debug("[511360] PCF 落盘失败: %s", e)


def fetch_pcf_day(code, d, timeout=15):
    """取某一交易日的 PCF（本地缓存优先）。非交易日/未发布返回 None。"""
    ds = d.strftime("%Y-%m-%d")
    path = _pcf_file(code, ds)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    url = HFT_PCF_URL.format(code=code, ds=d.strftime("%Y%m%d"))
    try:
        raw = http_open(url, timeout=timeout)
    except Exception:
        return None
    try:
        pcf = parse_pcf_xml(raw.decode("utf-8", "ignore"))
    except Exception:
        return None
    if pcf["date"] != ds:          # 官网未发布当日时可能回落到旧文件
        return None
    _save_pcf(pcf, code)
    return pcf


def load_pcf_local(code="511360", on_or_before=None):
    """读本地快照：on_or_before=YYYY-MM-DD 时取不晚于该日的最近一份（防前视）。"""
    if not os.path.isdir(DATA_DIR):
        return None
    fs = sorted(f for f in os.listdir(DATA_DIR)
                if f.startswith("pcf_%s_" % code) and f.endswith(".json"))
    if on_or_before:
        key = on_or_before.replace("-", "")
        fs = [f for f in fs if f.split("_")[-1][:8] <= key]
    if not fs:
        return None
    try:
        with open(os.path.join(DATA_DIR, fs[-1]), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_pcf_series(code="511360"):
    """本地全部 PCF 快照 {date: pcf}（升序），供历史核对页使用。"""
    if not os.path.isdir(DATA_DIR):
        return {}
    out = {}
    for fn in sorted(os.listdir(DATA_DIR)):
        if not (fn.startswith("pcf_%s_" % code) and fn.endswith(".json")):
            continue
        try:
            with open(os.path.join(DATA_DIR, fn), encoding="utf-8") as f:
                r = json.load(f)
            if r.get("date"):
                out[r["date"]] = r
        except Exception:
            continue
    return out


def fetch_pcf_series(code="511360", n=2, start=None):
    """最近 n 个交易日的 PCF（降序），借 PreTradingDay 逐日回溯。"""
    out = []
    d = start or date.today()
    guard = online = 0
    while len(out) < n and guard < 12 and online < 4:
        guard += 1
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        online += 1
        pcf = fetch_pcf_day(code, d)
        if pcf:
            out.append(pcf)
            pre = pcf.get("pre_date")
            d = (datetime.strptime(pre, "%Y-%m-%d").date() if pre
                 else d - timedelta(days=1))
        else:
            d -= timedelta(days=1)
    return out


# ════════════════════════════ 现券成交 / FR007 ════════════════════════════
def _term_years(s):
    if not s:
        return None
    m = re.fullmatch(r"\s*([\d\.]+)\s*([DY])\s*", str(s).strip().upper())
    if not m:
        return None
    v = float(m.group(1))
    return v / 365.0 if m.group(2) == "D" else v


def fetch_spot_deal(force=False, pages=1, page_size=5000):
    """货币网现券成交 {债券代码: {clean,ytm,bp,term,ts,name}}。

    与 511520 的 fetch_spot_deal（按简称索引）不同：短融/超短融 PCF 用 9 位代码，
    这里按代码索引；覆盖率通常约 1/4，属正常（低流动性短融整日无成交）。
    """
    now = time.monotonic()
    if not force and _spot_cache["v"] and now - _spot_cache["t"] < SPOT_TTL:
        return _spot_cache["v"]
    out = {}
    try:
        for pg in range(1, pages + 1):
            js = _post_json(CM_SPOT_URL, {
                "lang": "CN", "flag": "1", "bondName": "", "bondCode": "",
                "issueYear": "", "bondType": "", "excludeType": "", "bondTerm": "",
                "pageNum": str(pg), "pageSize": str(page_size),
            }, referer="https://www.chinamoney.com.cn/chinese/bkccpr/")
            recs = js.get("records") or []
            for r in recs:
                code = str(r.get("bondcode") or "").strip()
                try:
                    clean = float(r.get("dmiLatestRate"))
                    ytm = float(r.get("dmiLatestContraRate"))
                    bp = float(r.get("bpNum"))
                except (TypeError, ValueError):
                    continue
                if not code or clean <= 0:
                    continue
                ts = str(r.get("showDate") or "")
                old = out.get(code)
                if old and old["ts"] >= ts:
                    continue
                out[code] = {"clean": clean, "ytm": ytm, "bp": bp, "ts": ts,
                             "term": _term_years(r.get("termToMaturity")),
                             "name": r.get("abdAssetEncdShrtDesc")}
            if len(recs) < page_size:
                break
        _log_state("现券成交已恢复（%d 只）" % len(out), ok=True)
    except Exception as e:
        _log_state("货币网现券成交获取失败（自动重试）: %s" % str(e)[:120])
    if out:
        _spot_cache.update(t=now, v=out)
    return out


def fetch_frr007(force=False):
    """FR007 定盘利率：{date, fr007, series:{日期: 利率%}}，含历史序列。"""
    now = time.monotonic()
    if not force and _frr_cache["v"] and now - _frr_cache["t"] < FRR_TTL:
        return _frr_cache["v"]
    rows = []
    try:
        raw = _get_text(CM_FRR_CSV,
                        "https://www.chinamoney.com.cn/chinese/bkfrr/",
                        timeout=20)
        for line in raw.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 9 or not re.match(r"\d{4}-\d{2}-\d{2}", parts[0]):
                continue
            try:
                fr001 = float(parts[6]) if parts[6] else None
                fr007 = float(parts[7]) if parts[7] else None
            except ValueError:
                continue
            if fr007 is None and fr001:
                fr007 = fr001
            if fr007:
                rows.append((parts[0], fr007))
    except Exception as e:
        logger.debug("[511360] FR007 获取失败，用兜底 %.2f%%: %s",
                     DEFAULT_REPO_YTM, e)
    if not rows:
        return {"date": None, "fr007": DEFAULT_REPO_YTM, "series": {},
                "source": "兜底常量"}
    rows.sort()
    v = {"date": rows[-1][0], "fr007": rows[-1][1],
         "series": {d: x for d, x in rows}, "source": "中国货币网FR007"}
    _frr_cache.update(t=now, v=v)
    return v


def fr007_series():
    """历史页用：{日期: FR007(%)}，取不到则空 dict（上层用兜底常量）。"""
    return fetch_frr007().get("series") or {}


def fr007_on(day, series=None):
    """取不晚于 day 的最近 FR007（防前视）；无数据用兜底。"""
    s = series if series is not None else fr007_series()
    cand = [v for d, v in s.items() if d <= str(day)[:10]]
    return cand[-1] if cand else (min(s.values()) if s else DEFAULT_REPO_YTM)


def _latest_spot_date(spot):
    ds = []
    for v in (spot or {}).values():
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(v.get("ts") or ""))
        if m:
            ds.append(m.group(1))
    return max(ds) if ds else None


# ════════════════════════════ 算法说明（历史记录页展示）════════════════════════════
def algorithm_desc():
    return ("方法二 PCF 逐券篮子：海富通官网 PCF「替代金额/(数量×10)」= 成份券全价；"
            "逐券 ΔP = −修正久期 × Δy（成交 bp 中位数×0.9）、叠加逐券票息；"
            "按三季报经济结构折算：债券 82.80%（吃价格效应+票息）+ 买入返售 17.16%"
            "（FR007 生息、不吃价格效应）+ 其他 0.04%；以最新官方净值为锚只取篮子"
            "相对变动。篮子恒等式 (Σ替代金额+预估现金)/最小申赎单位 = 官方净值。"
            "盘后/周末价格效应归零，仍按票息+回购息计算，纯计算不做拟合。")


# ════════════════════════════ 唯一估值入口 ════════════════════════════
def estimate_511360_nav(nav_info=None, pcf=None, spot=None, fr007=None,
                        target_date=None, treasury_pct=None, prev_pcf=None):
    """511360 实时估值（唯一对外方法；换算法只需替换本函数）。

    参数（全部可注入 → 历史回测/自检不触发网络）
      nav_info     {nav, nav_date} 最新官方净值；None 走 bond_etf_valuation 缓存
      pcf          已解析的 PCF dict；None 走官网/本地快照
      spot         现券成交 dict；{} 表示只用 carry（历史回测）
      fr007        float(%)，或 fetch_frr007() 的 dict；None 自动取
      target_date  估值目标日 YYYY-MM-DD（默认今天）
      treasury_pct 国债指数 000012 涨幅%，仅作「指数涨幅」展示参考，不参与估值
      prev_pcf     上一交易日 PCF（可选，仅用于「已实现变动」参考）

    返回 {est, method, note, latest_nav, nav_date, index_nm, idx_chg,
          algo_id, algo_name, detail}
    """
    target = target_date or date.today().strftime("%Y-%m-%d")
    out = {"est": None, "method": ALGO_ID, "note": None,
           "latest_nav": None, "nav_date": None,
           "index_nm": "国债指数", "idx_chg": treasury_pct,
           "algo_id": ALGO_ID, "algo_name": ALGO_NAME, "detail": {}}

    # ---- 1) 官方净值（锚点）
    if nav_info is None:
        try:
            import bond_etf_valuation as _bv
            hist = _bv._nav_cache.get_history("511360", days=30)
            usable = [x for x in (hist or []) if x[0] <= target]
            if usable:
                nav_info = {"nav": usable[0][1], "nav_date": usable[0][0]}
        except Exception:
            nav_info = None
    # ---- 2) PCF
    if pcf is None:
        try:
            series = fetch_pcf_series("511360", n=2,
                                      start=datetime.strptime(target, "%Y-%m-%d").date())
        except Exception:
            series = []
        pcf = series[0] if series else None
        if prev_pcf is None and len(series) > 1:
            prev_pcf = series[1]
    if pcf is None:
        pcf = load_pcf_local("511360", on_or_before=target)
    if not pcf or not pcf.get("bonds"):
        out["note"] = "海富通 PCF 不可用（官网未发布且无本地快照）"
        return out
    if not nav_info or not nav_info.get("nav"):
        nav_info = {"nav": pcf.get("nav"),
                    "nav_date": pcf.get("pre_date") or pcf.get("date")}
    if not nav_info.get("nav"):
        out["note"] = "官方净值不可用"
        return out
    nav0 = float(nav_info["nav"])
    nav_date = nav_info["nav_date"]
    out["latest_nav"], out["nav_date"] = nav0, nav_date

    # ---- 3) 行情 / 回购利率
    if spot is None:
        spot = fetch_spot_deal()
    spot = spot or {}
    if fr007 is None:
        fr007 = fetch_frr007()
    if isinstance(fr007, dict):
        repo_ytm = float(fr007.get("fr007") or DEFAULT_REPO_YTM)
        repo_src = fr007.get("source") or "FR007"
    else:
        repo_ytm = float(fr007)
        repo_src = "外部注入 FR007"

    # ---- 4) 日期对齐
    try:
        days = max(1, min((datetime.strptime(target, "%Y-%m-%d").date()
                           - datetime.strptime(str(nav_date)[:10], "%Y-%m-%d").date()
                           ).days, 10))
    except (ValueError, TypeError):
        days = 1
    sp_date = _latest_spot_date(spot)
    no_new_quote = bool(sp_date and nav_date and sp_date <= str(nav_date)[:10])

    # ---- 5) 逐券：全价基准、权重、久期、成交信息
    unit = float(pcf.get("unit") or 2000.0)
    est_cash = float(pcf.get("est_cash") or 0.0)
    bonds = [dict(b) for b in pcf["bonds"] if b.get("qty", 0) > 0]
    total_amt = sum(b["amount"] for b in bonds)
    if total_amt <= 0 or not bonds:
        out["note"] = "PCF 替代金额合计为 0，清单异常"
        return out
    traded = []
    for b in bonds:
        b["px_base"] = b["amount"] / (b["qty"] * 10.0)
        b["w"] = b["amount"] / total_amt
        s = spot.get(b["code"]) or {}
        b["clean"] = s.get("clean")
        b["ytm"] = s.get("ytm")
        b["dy_bp"] = s.get("bp")
        b["term"] = s.get("term")
        y = (b["ytm"] if b["ytm"] else DEFAULT_YTM) / 100.0
        b["dur"] = (b["term"] / (1 + y)) if b["term"] else DEFAULT_DUR
        b["traded"] = bool(s)
        if s:
            traded.append(b)
    dur_w = sum(b["w"] * b["dur"] for b in bonds)

    # ---- 6) Δy：成交 bp 中位数（抗短融整数报价噪声），按 DY_WEIGHT 折减
    valid = [(b["amount"], b["dy_bp"]) for b in bonds
             if b["dy_bp"] is not None and abs(b["dy_bp"]) <= DY_LIMIT_BP]
    coverage = len(valid) / len(bonds) if bonds else 0.0
    if no_new_quote:
        dy_raw = dy = 0.0
        dy_src = "无新增行情（价格效应归零，仅计票息）"
    elif valid:
        dy_raw = statistics.median([v for _, v in valid])
        dy = dy_raw * DY_WEIGHT
        dy_src = ("成交行情中位数 %+.2fbp（%d/%d 只成交，覆盖率 %.0f%%）× %.2f"
                  % (dy_raw, len(valid), len(bonds), coverage * 100, DY_WEIGHT))
    else:
        dy_raw = dy = 0.0
        dy_src = "无成交行情（按 0 处理，仅 carry）"

    # ---- 7) carry：债券按组合收益率，回购按 FR007
    if traded:
        wsum = sum(b["w"] for b in traded)
        ytm_w = (sum(b["w"] * b["ytm"] for b in traded) / wsum
                 if wsum else None)
    else:
        ytm_w = None
    ytm_bond = ytm_w or DEFAULT_YTM
    w_b, w_r = BOND_W, REPO_W
    w_o = max(0.0, 1.0 - w_b - w_r)
    c_b_day = nav0 * w_b * ytm_bond / 100.0 / ACC_YEAR_DAYS
    c_r_day = nav0 * w_r * repo_ytm / 100.0 / ACC_YEAR_DAYS
    carry_bond_rate = (ytm_bond / 100.0 / ACC_YEAR_DAYS) * days

    # ---- 8) 逐券重估
    v_base = v_est = 0.0
    rows = []
    for b in bonds:
        ok = (not no_new_quote and b["dy_bp"] is not None
              and abs(b["dy_bp"]) <= DY_LIMIT_BP)
        use_dy = b["dy_bp"] * DY_WEIGHT if ok else dy
        b["dy_used"] = use_dy
        b["dp_pct"] = -b["dur"] * use_dy / 100.0
        b["px_est"] = b["px_base"] * (1 + carry_bond_rate + b["dp_pct"] / 100.0)
        b["src"] = ("仅carry" if no_new_quote else
                    ("本券Δy" if ok else "组合Δy"))
        v_base += b["qty"] * 10 * b["px_base"]
        v_est += b["qty"] * 10 * b["px_est"]
        rows.append({"代码": b["code"], "简称": b["name"], "数量": b["qty"],
                     "权重": round(b["w"], 6), "久期": round(b["dur"], 4),
                     "到期收益率%": b["ytm"], "Δy_bp": round(use_dy, 4),
                     "PCF全价": round(b["px_base"], 6),
                     "估值全价": round(b["px_est"], 6),
                     "ΔP%": round(b["dp_pct"], 6), "来源": b["src"]})

    # ---- 9) 经济结构汇总（锚定最新官方净值，只取篮子相对变动）
    nav_basket = (nav0 * (v_est + est_cash) / (v_base + est_cash)
                  if (v_base + est_cash) > 0 else nav0)
    repo_part = nav0 * w_r * (1 + repo_ytm / 100.0 / ACC_YEAR_DAYS * days)
    other_part = nav0 * w_o
    nav_est = w_b * nav_basket + repo_part + other_part
    out["est"] = round(nav_est, 4)
    out["method"] = "pcf_basket_511360_carry_only" if no_new_quote else ALGO_ID
    if no_new_quote:
        out["note"] = ("最新行情 %s 已被官方净值 %s 消化，价格效应归零；"
                       "按逐券票息 + FR007 回购息计算 %.4f（纯计算，不冻结/不拟合）"
                       % (sp_date, nav_date, nav_est))
    elif pcf.get("stale"):
        out["note"] = "官网当日 PCF 未发布/不可达，用本地快照 %s" % pcf.get("date")

    implied = (v_base + est_cash) / unit if unit else None
    basket_diff_bp = ((implied / nav0 - 1) * 10000) if implied and nav0 else None
    realized = None
    if prev_pcf and prev_pcf.get("nav_per_cu") and pcf.get("nav_per_cu"):
        realized = {"amount": (pcf["nav_per_cu"] - prev_pcf["nav_per_cu"]) / unit,
                    "date": nav_date, "prev_date": prev_pcf.get("pre_date")}

    out["detail"] = {
        "算法": {"id": ALGO_ID, "名称": ALGO_NAME, "版本": ALGO_VERSION,
                 "入口": ALGO_ENTRY, "说明": algorithm_desc()},
        "估值状态": ("盘后/周末：无新增行情，价格效应归零，仅按票息+回购息计算"
                   if no_new_quote else "盘中：逐券按成交 Δy + 票息重估全价"),
        "日期对齐": {"官方净值日": nav_date, "官方净值": nav0,
                     "行情日期": sp_date, "PCF日期": pcf.get("date"),
                     "PCF基准日": pcf.get("pre_date"), "估值目标日": target,
                     "计息天数": days,
                     "说明": "价格效应只叠加比官方净值更新的行情；官方净值仅作锚点，"
                             "不作为 est 直接输出"},
        "篮子校验": {"隐含净值": round(implied, 6) if implied else None,
                     "官方净值": nav0,
                     "偏差bp": round(basket_diff_bp, 3)
                     if basket_diff_bp is not None else None,
                     "最小申赎单位": unit, "预估现金": est_cash,
                     "Σ替代金额": round(total_amt, 2)},
        "资产结构": {"债券": w_b, "买入返售": w_r, "其他": round(w_o, 4),
                     "PCF实测债券占比": round(total_amt / (total_amt + est_cash), 6)
                     if (total_amt + est_cash) else None,
                     "口径": "2026 年三季报；PCF 篮子只取相对变动"},
        "驱动变量": {"Δy_bp": round(dy, 4), "Δy原始中位数bp": round(dy_raw, 4),
                     "Δy来源": dy_src, "成交覆盖率": round(coverage, 4),
                     "成交只数": len(traded), "成份券数": len(bonds),
                     "组合加权久期": round(dur_w, 4),
                     "债券组合收益率%": round(ytm_bond, 4),
                     "收益率来源": ("成交加权 %d 只" % len(traded)) if ytm_w
                                   else "兜底常量",
                     "FR007%": repo_ytm, "FR007来源": repo_src,
                     "债券每日carry元/份": round(c_b_day, 6),
                     "回购每日carry元/份": round(c_r_day, 6),
                     "债券累计carry率": round(carry_bond_rate, 8)},
        "仓位": {"债券篮子净值": round(nav_basket, 6),
                 "回购部分净值": round(repo_part, 6),
                 "其他部分": round(other_part, 6),
                 "估值结果": round(nav_est, 6)},
        "两日PCF实际": realized,
        "PCF来源": pcf.get("source"),
        "成份券": rows,
    }
    _log_state("PCF 逐券估值正常（%d 只成份券）" % len(bonds), ok=True)
    return out


def clear_caches():
    with _pcf_lock:
        _pcf_cache.update(t=0.0, v=None)
    _spot_cache.update(t=0.0, v=None)
    _frr_cache.update(t=0.0, v=None)
    _state["failed"] = False


# ════════════════════════════ 自检 ════════════════════════════
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    r = estimate_511360_nav()
    print("est=%s  method=%s  nav0=%s[%s]" %
          (r["est"], r["method"], r["latest_nav"], r["nav_date"]))
    if r.get("note"):
        print("note:", r["note"])
    d = r.get("detail") or {}
    print("篮子校验:", d.get("篮子校验"))
    print("驱动变量:", d.get("驱动变量"))
    print("仓位:", d.get("仓位"))
