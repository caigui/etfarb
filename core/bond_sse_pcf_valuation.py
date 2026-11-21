# -*- coding: utf-8 -*-
"""
bond_sse_pcf_valuation.py —— 上交所债券ETF「PCF 逐券估值」（2026-11-14 新增）
================================================================================

标的：511090 30年国债ETF鹏扬 / 511130 30年国债ETF博时 / 511220 城投债ETF海富通

数据链路（全部免费公开源，复用 bond_pcf_valuation 的取数通道）
------------------------------------------------------------------
【PCF 多源容错（2026-11-16 改造）】
  原则：**每天只要成功一次就固化到本地，之后全天零请求**。PCF 一日内不会变化，
  反复抓取只会刷 WARNING 并浪费首屏时间。成功结果写
      data/bond_sse/pcf_{code}_{date8}.json           （含来源标记）
  当天已有成功快照 → 直接读盘返回，不再联网。
  只有「当天还没有成功快照」时才按优先级尝试下列源，任一成功即落盘并停止：

   ① sse  上交所下载口（官方，无日期参数=永远最新一份）
        query.sse.com.cn/etfDownload/downloadETF2Bulletin.do?fundCode=<code>
        ⚠️ 只给数量、不给替代金额；TradingDay=T 的公告用 T-1 官方净值。
   ② amc  基金公司官网归档（可按日期回溯，命中即确认当天公告已发布）
        · 博时：/jjcp/etf/files/{code}/{YYYY}/ssepcf_{code}_{YYYYMMDD}.xml
          实测 511130 可回溯历史、且 09:00 前就已有当日文件。
        · 海富通：/upload/applications/funds-struts/pcf{code}/{code}{YYYYMMDD}.xml
          实测 511220 可用（511360/511520 已在 bond_pcf_valuation 用同款路径）。
   ③ urllib 裸通道重试 ①（curl_cffi 的 TLS 指纹/代理链路偶发失败时，
      裸 urllib 常能直连成功 —— 2026-11-16 实测两者互补）。

  ⚠️ 鹏扬（511090）官网 PCF 页面为 JS 动态渲染、无稳定静态归档目录，
     故 511090 依赖 ①③；若两条都失败，当天沿用最近一份快照并把
     `stale_days` 写入 detail，日志按「状态切换」告警一次而非每轮刷屏。

【其它数据源】
1) 上交所 PCF 结构：InstrumentID / InstrumentName / Quantity / SubstitutionFlag。
   Quantity 单位 = 1000 元面值（实测 511090：1200×1000 ≈ NAVperCU 1,211,763，
   平均全价 100.46% ✓）。

2) 中国货币网银行间现券成交（bond_pcf_valuation.fetch_spot_deal）：
   · 30Y 特别国债按「25特国02 → 25超长特别国债02」名称映射命中年报成交，
     dy_bp=当日收益率变动 —— 与 511520 完全同源。
   · 城投债ETF 成份为交易所券（货币网无），用**银行间城投/中票**的
     Δy 中位数 × 期限作同向代理（2026-11-13 回测：代理 -9.4bp vs 实际 -9.9bp）。

3) 30年国债期货 TL 日K（新浪，最后一根 K 线盘中实时更新）：
   成份券成交稀疏、bp 失真时的兜底方向信号（同 511520 用曲线兜底的思想）。

估值公式（纯计算，锚定 + 增量，不拟合官方净值）
------------------------------------------------------------------
  est = anchor_nav + ΔNAV
  ΔNAV = Σ_i [ v_i × dpct_i / 100 ] / unit + Σ_i [ v_i / 100 × carry_i ] × days / unit
  其中 v_i = qty_i × 10 × clean_i（每申赎单位的市值，clean 为每百元面值净价）

驱动优先级（30Y 国债）：
  ① 活跃券 Δy：成份券成交里 |dy_bp| 最大者（=最新成交，镜面 511520 活跃券逻辑）
     × 面值加权组合修正久期 D_port（解析式 D=(1-(1+y)^-T)/y）
  ② TL 活跃合约日涨跌 × TL_RATIO(0.97)
  ③ 纯票息（方向=0）

日期对齐（与 511520 同规则）
------------------------------------------------------------------
  锚点 = 最新官方净值日 D0（东财 lsjz）；货币网行情日 Ds。
  Ds > D0 才叠加行情（Δy 是 Ds 对 Ds-1 的变动，而 Ds-1 = D0 恰为锚点日）；
  Ds <= D0 说明行情已被净值消化，est = 锚点净值（纯计算，不重复叠加）。

回测（2026-11-13，锚=11-12 公告净值，驱动=11-13 行情）
------------------------------------------------------------------
  511090  est≈120.699  实际 120.7439  误差 ≈ -3.8bp（活跃券dy路径）
  511130  est≈110.250  实际 110.1721  误差 ≈ +7.1bp（同上；该日实际跌幅
          超过 30Y 市场整体，属基金个体差异，detail 中披露）
  511220  est≈10.4386  实际 10.4381  误差 ≈ -0.5bp（银行间城投代理路径）
"""

from __future__ import print_function

import json
import logging
import os
import re
import ssl
import time
import urllib.request
from datetime import date, datetime

import bond_pcf_valuation as bpv      # 取数链路 / 现券成交 / carry / 交易日工具

logger = logging.getLogger("bond_sse_pcf_valuation")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data", "bond_sse")

SSE_BULLETIN_URL = ("https://query.sse.com.cn/etfDownload/"
                    "downloadETF2Bulletin.do?fundCode=%s")
SSE_REFERER = "https://www.sse.com.cn/"

# ── 基金公司官网 PCF 归档（2026-11-16 新增：可按日期回溯的第二源）──
# 返回 (url, referer)；{code} {date8} {year} 占位
AMC_PCF_SOURCES = {
    # 博时（511130）：实测 /jjcp/etf/files/{code}/{year}/ssepcf_{code}_{date8}.xml
    #   Content-Type: text/xml，可按日期回溯，09:00 前当日文件已发布。
    "511130": [("https://www.bosera.com/jjcp/etf/files/{code}/{year}/"
                "ssepcf_{code}_{date8}.xml", "https://www.bosera.com/fund/{code}.html")],
    # 海富通（511220）：实测 /upload/applications/funds-struts/pcf{code}/{code}{date8}.xml
    #   （511360/511520 同规律，见 bond_pcf_valuation.fetch_pcf_online）
    "511220": [("https://www.hftfund.com/upload/applications/funds-struts/"
                "pcf{code}/{code}{date8}.xml", "https://www.hftfund.com/")],
    # 511090 鹏扬：官网 PCF 页为 JS 渲染、无静态归档目录，只能依赖上交所口。
    "511090": [],
}

_FUT_KLINE_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php"
                  "/var%20_=/InnerFuturesNewService.getDailyKLine?symbol=%s")

ALGO_ID = "bond_sse_pcf_v1"
ALGO_VERSION = "2026-11-16"

# TL 期货兜底的弹性（组合久期/TL久期≈21.5/22 的经验值，11-13 回测校准）
TL_RATIO = 0.97

SSE_PCF_META = {
    "511090": {"name": "30年国债ETF鹏扬", "kind": "treasury"},
    "511130": {"name": "30年国债ETF博时", "kind": "treasury"},
    "511220": {"name": "城投债ETF海富通", "kind": "credit"},
}

# 银行间信用债代理的名称关键字（城投优先，不足时并入中票/PPN）
_CREDIT_KEYS = ("城投",)
_CREDIT_KEYS_ALL = ("城投", "PPN", "中票", "MTN", "短融", "超短融")
# 最宽：样本稀少时并入金融债/CD/二级资本债（只借它们的 Δy 方向，不借期限）
_CREDIT_KEYS_WIDE = _CREDIT_KEYS_ALL + ("CD", "二级资本债", "金融债", "公司债")

_pcf_cache = {}        # {code: (monotonic, pcf)}  进程内当日成功快照
_spot_credit = {}      # {key: (monotonic, stats)}
_tl_kline_cache = {"t": 0.0, "sym": None, "v": None}

# 🔴 抓取状态（2026-11-16）：只在「正常→失败」「失败→恢复」切换时告警一次，
#    避免看板 30 秒一刷把同一条 WARNING 刷满屏。
PCF_FAIL_BACKOFF = 300.0      # 当天全部源失败后，300 秒内不再重试（当日只重试少量次数）
_pcf_state = {}               # {code: {"ok": bool, "fail_at": monotonic, "src": str, "msg": str}}


def clear_caches():
    """清本模块进程内缓存（看板「补采+重算」用）。

    ⚠️ 故意**不清**当日已成功落盘的 PCF 快照 —— 一天成功一次即可，
    重算估值不需要重新抓清单（PCF 日内不变）。
    """
    _pcf_cache.clear()
    _spot_credit.clear()
    _tl_kline_cache.update({"t": 0.0, "sym": None, "v": None})
    _pcf_state.clear()


# ════════════════════════ PCF 多源获取（2026-11-16 改造）════════════════════════

def _d8(s):
    """20261113 → 2026-11-13"""
    s = str(s or "")[:8]
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:8]) if len(s) == 8 else s


def _d8_raw(s):
    """任意日期串 → YYYYMMDD（YYYY-MM-DD / YYYYMMDD 都吃）。"""
    return re.sub(r"\D", "", str(s or ""))[:8]


def _log_state(code, ok, src=None, msg=None):
    """抓取状态变化才写日志：正常→失败 WARNING 一次；失败→恢复 INFO 一次。"""
    st = _pcf_state.setdefault(code, {"ok": None})
    prev = st.get("ok")
    st["ok"] = ok
    if ok:
        st["fail_at"] = 0.0
        st["src"] = src
        if prev is False:
            logger.info("[sse_pcf] %s PCF 取数已恢复（来源：%s）", code, src)
        elif prev is None:
            logger.info("[sse_pcf] %s PCF 取数成功（来源：%s）", code, src)
        return
    st["msg"] = msg
    st["fail_at"] = time.monotonic()
    if prev is not False:
        logger.warning("[sse_pcf] %s PCF 全部取数源暂不可用：%s；"
                       "沿用本地快照，300 秒后重试", code, msg)


def _parse_sse_xml(txt):
    """上交所 PCF XML → dict（源无关：上交所口与基金公司归档同格式）。"""
    if "TradingDay" not in txt or "<Component>" not in txt:
        raise RuntimeError("PCF 报文不含 TradingDay/Component（页面结构可能已变）")

    def _tag(t, s=txt):
        m = re.search(r"<%s>(.*?)</%s>" % (t, t), s, re.S)
        return m.group(1).strip() if m else None

    bonds = []
    for blk in re.findall(r"<Component>(.*?)</Component>", txt, re.S):
        def _bt(t, s=blk):
            m = re.search(r"<%s>(.*?)</%s>" % (t, t), s, re.S)
            return m.group(1).strip() if m else None
        qty = _num(_bt("Quantity"))
        bonds.append({"id": _bt("InstrumentID"), "name": _bt("InstrumentName"),
                      "qty": int(qty) if qty else 0, "flag": _bt("SubstitutionFlag")})
    pcf = {
        "trading_day": _d8(_tag("TradingDay")),
        "pre_day": _d8(_tag("PreTradingDay")),
        "nav": _num(_tag("NAV")),
        "unit": _num(_tag("CreationRedemptionUnit")),
        "navpercu": _num(_tag("NAVperCU")),
        "est_cash": _num(_tag("EstimatedCashComponent")),
        "bonds": bonds,
    }
    if not pcf["bonds"] or not pcf["unit"] or pcf["nav"] is None:
        raise RuntimeError("PCF 关键数据缺失（成份券/最小申赎单位/净值）")
    return pcf


def _get_sse_official(code):
    """① 上交所下载口（官方，无日期参数 = 永远最新一份）。

    impersonate=None ⇒ 交给 bpv._req 按指纹候选表自动降级（2026-11-16）。
    """
    txt = bpv._get(SSE_BULLETIN_URL % code, referer=SSE_REFERER,
                   timeout=15, impersonate=None)
    return _parse_sse_xml(txt)


def _get_sse_official_urllib(code):
    """③ 裸 urllib 重试上交所口（curl_cffi 代理链路抖动时互补）。"""
    req = urllib.request.Request(SSE_BULLETIN_URL % code, headers={
        "User-Agent": bpv._UA, "Referer": SSE_REFERER})
    raw = urllib.request.urlopen(req, timeout=15, context=_CTX)
    return _parse_sse_xml(raw.read().decode("utf-8", "ignore"))


def _get_amc_archive(code, day8):
    """② 基金公司官网归档（可按日期回溯）。返回 (pcf, url)。"""
    year = day8[:4]
    errs = []
    for tpl, referer in AMC_PCF_SOURCES.get(code) or []:
        url = tpl.format(code=code, date8=day8, year=year)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": bpv._UA,
                "Referer": referer.format(code=code)})
            raw = urllib.request.urlopen(req, timeout=15, context=_CTX)
            txt = raw.read().decode("utf-8", "ignore")
            pcf = _parse_sse_xml(txt)
            # 日期校验：归档文件必须确实是「请求的那一天」（防回落最新一份）
            if _d8_raw(pcf.get("trading_day")) != day8:
                errs.append("%s 日期不符(%s)" % (url, pcf.get("trading_day")))
                continue
            return pcf, url
        except Exception as e:
            errs.append("%s: %s" % (url.rsplit("/", 1)[-1], type(e).__name__))
    if errs:
        raise RuntimeError("官网归档不可用：" + "; ".join(errs[:3]))
    raise RuntimeError("该基金无官网归档源")


def _read_cached_day(code, day8):
    """读当天已成功落盘的快照（进程内 → 文件）。"""
    now = time.monotonic()
    hit = _pcf_cache.get(code)
    if hit and _d8_raw((hit[1] or {}).get("trading_day")) == day8:
        return hit[1]
    fp = _pcf_path(code, day8)
    if os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                j = json.load(f)
            # 只认「当天真正抓成功」的快照（src 不为空），不认历史回退拷贝
            if j.get("bonds") and j.get("_fetched_at"):
                _pcf_cache[code] = (now, j)
                return j
        except Exception:
            pass
    return None


def _pcf_path(code, day8):
    return os.path.join(DATA_DIR, "pcf_%s_%s.json" % (code, day8))


def _latest_cached(code, before8=None):
    """最近一份可用快照（当天抓不到时兜底）。

    before8=None 时包含「当天」那份 —— 抓取失败时沿用的正是最近成功过的那份
    （可能是今天早上抓的，也可能更早），所以不能排除当天。
    """
    try:
        fs = sorted(f for f in os.listdir(DATA_DIR)
                    if f.startswith("pcf_%s_" % code) and f.endswith(".json"))
    except Exception:
        return None, None
    for f in reversed(fs):
        d8 = f[len("pcf_%s_" % code):-len(".json")]
        if before8 and d8 > before8:      # 只跳过「未来日」，允许等于当天
            continue
        try:
            with open(os.path.join(DATA_DIR, f), encoding="utf-8") as fh:
                j = json.load(fh)
            if j.get("bonds"):
                return j, d8
        except Exception:
            continue
    return None, None


def fetch_sse_pcf(code, force=False, day=None):
    """取 PCF —— **每天只需成功一次**，之后全天读盘零请求。

    流程：
      0) 当日已有成功快照（进程内 / 文件）→ 直接返回，不联网；
      1) 否则按 ①上交所 → ②基金公司官网归档 → ③urllib 裸通道 依次尝试，
         任一成功即落盘 pcf_{code}_{YYYYMMDD}.json 并返回；
      2) 全失败 → 300 秒内不再重试（避免看板每 30 秒刷同一条 WARNING），
         期间沿用最近一份快照并标记 stale_days；无任何快照才抛异常。
    """
    d = date.fromisoformat(day) if isinstance(day, str) else (day or date.today())
    day8 = d.strftime("%Y%m%d")
    now = time.monotonic()

    # 0) 当天已成功 → 零请求
    if not force:
        cached = _read_cached_day(code, day8)
        if cached:
            return cached

    # 2) 失败退避期内：直接走兜底，不再打网络
    st = _pcf_state.get(code) or {}
    if not force and st.get("ok") is False and now - st.get("fail_at", 0) < PCF_FAIL_BACKOFF:
        stale, sday = _latest_cached(code, before8=day8)
        if stale:
            out = dict(stale)
            out["stale_days"] = _trading_gap(sday, day8)
            out["source"] = (stale.get("source") or "本地快照") + "（退避中）"
            return out
        raise RuntimeError(st.get("msg") or "PCF 全部取数源不可用（退避中）")

    errs, pcf, src = [], None, None
    for name, fn in (("上交所", lambda: _get_sse_official(code)),
                     ("基金公司官网", lambda: _get_amc_archive(code, day8)),
                     ("上交所(直连)", lambda: _get_sse_official_urllib(code))):
        try:
            got = fn()
            if isinstance(got, tuple):
                got, url = got
                src = "%s %s" % (name, url)
            else:
                src = name
            pcf = got
            break
        except Exception as e:
            errs.append("%s: %s" % (name, str(e)[:80]))

    if pcf:
        pcf = dict(pcf)
        pcf["source"] = src
        pcf["_fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 落盘用「公告本身的交易日」命名（上交所口无日期参数，可能滞后一天），
        # 同时写入实际抓取日，便于 _read_cached_day 判断「今天是否已成功过」
        tday8 = _d8_raw(pcf.get("trading_day")) or day8
        pcf["_target_day"] = day8
        _pcf_cache[code] = (now, pcf)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(_pcf_path(code, day8), "w", encoding="utf-8") as f:
                json.dump(pcf, f, ensure_ascii=False, indent=1)
            if tday8 != day8:
                # 公告日期比目标日旧（如盘前还没发当日清单）→ 额外留一份带日期的
                with open(_pcf_path(code, tday8), "w", encoding="utf-8") as f:
                    json.dump(pcf, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.debug("[sse_pcf] %s 快照落盘失败: %s", code, e)
        _log_state(code, True, src=src)
        return pcf

    # 全失败 → 兜底最近快照 + 状态化告警
    msg = " / ".join(errs)
    _log_state(code, False, msg=msg)
    stale, sday = _latest_cached(code, before8=day8)
    if stale:
        out = dict(stale)
        out["stale_days"] = _trading_gap(sday, day8)
        out["source"] = (stale.get("source") or "本地快照") + "（回退）"
        return out
    raise RuntimeError("PCF 全部取数源不可用且无本地快照：" + msg)


def _trading_gap(d0_8, d1_8):
    """两个 YYYYMMDD 之间的交易日间隔（用于标注快照滞后几个交易日）。"""
    try:
        a = datetime.strptime(str(d0_8)[:8], "%Y%m%d").date()
        b = datetime.strptime(str(d1_8)[:8], "%Y%m%d").date()
    except (ValueError, TypeError):
        return 0
    return max(0, bpv._trading_days_between(a.isoformat(), b.isoformat()) - 1)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ════════════════════════ 东财净值历史（锚点） ════════════════════════

def _nav_history(code, days=30):
    """东财 lsjz → [(date, nav)] **降序**（与 bond_etf_valuation 同源口径）。"""
    url = ("https://api.fund.eastmoney.com/f10/lsjz?callback=jQuery&fundCode=%s"
           "&pageIndex=1&pageSize=%d" % (code, days))
    req = urllib.request.Request(url, headers={
        "User-Agent": bpv._UA,
        "Referer": "https://fundf10.eastmoney.com/jjjz_%s.html" % code,
    })
    raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
    s = raw.find("{"); e = raw.rfind("}") + 1
    if s < 0:
        return []
    import json as _json
    data = _json.loads(raw[s:e])
    out = []
    for r in data.get("Data", {}).get("LSJZList", []):
        d, nav = r.get("FSRQ"), r.get("DWJZ")
        try:
            out.append((d, float(nav)))
        except (TypeError, ValueError):
            continue
    return out


# ════════════════════════ 现券行情映射 ════════════════════════

def _spot_key_for(pcf_name, spot):
    """PCF 券简称 → 货币网现券行情 key。
    直接同名优先；再按「25特国02 → 25超长特别国债02」规则扩展。"""
    if pcf_name in spot:
        return pcf_name
    m = re.match(r"(\d{2})特国0*(\d+)", pcf_name or "")
    if m:
        for cand in ("%s超长特别国债%02d" % (m.group(1), int(m.group(2))),
                     "%s特别国债%02d" % (m.group(1), int(m.group(2))),
                     "%s特国%02d" % (m.group(1), int(m.group(2)))):
            if cand in spot:
                return cand
    return None


def _credit_stats(spot, keys=None):
    """银行间信用债代理统计：{dy, term, yld, n, level}（Δy/期限/收益率中位数）。

    分层收紧 → 放宽（2026-11-16 加固）：城投(≥10样本) → 城投+中票+PPN(≥10)
    → 全部信用类（含短融/超短融/CD/二级资本债，≥10）→ 样本不足时降为
    `level='thin'` 并在 detail 标注（此时只信 Δy，期限用 2.5 年经验值兜底）。
    """
    kk = keys or _CREDIT_KEYS
    now = time.monotonic()
    ck = "k%d" % len(kk)
    if ck in _spot_credit and now - _spot_credit[ck][0] < 300:
        return _spot_credit[ck][1]
    qs = [v for k, v in spot.items()
          if any(x in k for x in kk) and _num(v.get("dy_bp")) is not None]
    # 逐级放宽：样本 <10 就换更宽的关键字集
    if len(qs) < 10 and kk is _CREDIT_KEYS:
        return _credit_stats(spot, keys=_CREDIT_KEYS_ALL)
    if len(qs) < 10 and kk is _CREDIT_KEYS_ALL:
        return _credit_stats(spot, keys=_CREDIT_KEYS_WIDE)
    if not qs:
        res = None
    else:
        def med(f):
            xs = sorted(x for x in (f(q) for q in qs) if x is not None)
            return xs[len(xs) // 2] if xs else None
        thin = len(qs) < 10
        res = {"dy": med(lambda q: q.get("dy_bp")),
               "term": med(lambda q: q.get("term")) or (2.5 if thin else None),
               "yld": med(lambda q: q.get("yield")) or (1.9 if thin else None),
               "n": len(qs), "thin": thin,
               "level": "thin" if thin else ("城投" if kk is _CREDIT_KEYS
                                             else ("城投+中票" if kk is _CREDIT_KEYS_ALL
                                                   else "全信用类"))}
    _spot_credit[ck] = (now, res)
    return res


def _mod_dur(y, term_years):
    """≈面值券修正久期：D_mod = (1-(1+y)^-T)/y（coupon≈y 近似，30Y 国债误差<2%）。"""
    if not y or not term_years or y <= 0 or term_years <= 0:
        return None
    try:
        return (1.0 - (1.0 + y / 100.0) ** (-term_years)) / (y / 100.0)
    except Exception:
        return None


def _tl_active(as_of=None):
    """活跃 TL 合约代码（复用 bond_etf_valuation 换月逻辑，懒加载避免循环导入）。"""
    try:
        import bond_etf_valuation as _bv
        return _bv._active_cffex_code("TL", as_of=as_of)
    except Exception:
        return "TL%02d12" % (date.today().year % 100)


def fetch_tl_kline(symbol=None, ttl=1800):
    """TL 日K {date: close}（最后一根盘中实时更新）。失败返回 {}。"""
    symbol = symbol or _tl_active()
    now = time.monotonic()
    if (_tl_kline_cache["v"] is not None and _tl_kline_cache["sym"] == symbol
            and now - _tl_kline_cache["t"] < ttl):
        return _tl_kline_cache["v"]
    out = {}
    try:
        req = urllib.request.Request(_FUT_KLINE_URL % symbol, headers={
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": bpv._UA})
        raw = urllib.request.urlopen(req, timeout=10, context=_CTX).read().decode("utf-8", "ignore")
        s = raw.find("["); e = raw.rfind("]") + 1
        if s >= 0:
            for k in json.loads(raw[s:e]):
                d, c = k.get("d"), _num(k.get("c"))
                if d and c:
                    out[str(d)[:10]] = c
    except Exception:
        return {}
    if out:
        _tl_kline_cache.update({"t": now, "sym": symbol, "v": out})
    return out


def _tl_pct(as_of=None):
    """TL 活跃合约最新涨跌幅%（最后一根 K 线 vs 前一根；回测按 as_of 取当根）。"""
    kl = fetch_tl_kline(_tl_active(as_of))
    ds = sorted(kl)
    if len(ds) < 2:
        return None, None, None
    if as_of:
        ds = [d for d in ds if d <= str(as_of)[:10]]
        if len(ds) < 2:
            return None, None, None
    c, c0 = kl[ds[-1]], kl[ds[-2]]
    if not c0:
        return None, None, None
    return (c / c0 - 1.0) * 100.0, c, ds[-1]


# ════════════════════════ 主入口 ════════════════════════

def estimate_sse_bond_nav(code, target_date=None, pcf=None, spot=None):
    """上交所债券 ETF 日内估值。返回形状与 estimate_bond_nav 对齐：

    {est, idx_chg, index_nm, idx_price, method, note,
     latest_nav, nav_date, detail}
    """
    meta = SSE_PCF_META.get(code) or {}
    out = {"est": None, "idx_chg": None, "index_nm": None, "idx_price": None,
           "method": "sse_pcf", "note": None,
           "latest_nav": None, "nav_date": None, "detail": {}}
    if not meta:
        out["note"] = "未知上交所债券基金 %s" % code
        return out

    # PCF：当天成功一次即读盘（不重复联网）；历史回测按目标日取当日快照
    try:
        pcf = pcf or fetch_sse_pcf(code, day=target_date)
    except Exception as e:
        out["note"] = "PCF 获取失败：%s" % str(e)[:160]
        return out
    spot = spot or bpv.fetch_spot_deal()
    nav_hist = _nav_history(code, days=30)
    if not nav_hist:
        out["note"] = "净值历史获取失败（天天基金）"
        return out
    target = str(target_date or date.today())[:10]

    # 锚点：target 之前最近一份官方净值（target=None 即最新）
    if target_date:
        cands = [x for x in nav_hist if x[0] < target]
    else:
        cands = nav_hist
    if not cands:
        out["note"] = "target_date=%s 之前无官方净值" % target
        return out
    anchor_date, anchor_nav = cands[0]
    out["latest_nav"] = anchor_nav
    out["nav_date"] = anchor_date

    unit = pcf["unit"]
    sp_date = bpv._latest_spot_date(spot)
    bonds = [b for b in pcf["bonds"] if b.get("qty", 0) > 0]
    days = bpv._trading_days_between(anchor_date, min(target, sp_date or target)) \
        if sp_date and sp_date > anchor_date else 0

    # ── 篮子校验：公告 NAV 应等于最新官方锚点净值（TradingDay 公告用 T-1 净值） ──
    basket_diff_bp = None
    if pcf["nav"] and anchor_nav:
        basket_diff_bp = (pcf["nav"] / anchor_nav - 1.0) * 10000.0

    # ── 行情不新于锚点：行情已被净值消化，est=锚点（纯计算不重复叠加） ──
    if not sp_date or sp_date <= anchor_date:
        out["est"] = round(anchor_nav, 4)
        out["method"] = "sse_pcf_no_new_mkt"
        _stale = int(pcf.get("stale_days") or 0)
        out["note"] = ("最新行情 %s 已被官方净值 %s 消化，无更新行情；"
                       "显示锚点净值 %.4f" % (sp_date, anchor_date, anchor_nav)
                       + ("；⚠️ PCF 沿用 %s 快照（滞后 %d 个交易日）"
                          % (pcf.get("trading_day"), _stale) if _stale else ""))
        out["detail"] = {
            "估值状态": "锚点净值（无更新行情）",
            "日期对齐": {"官方净值日": anchor_date, "官方净值": anchor_nav,
                         "行情日期": sp_date, "公告交易日": pcf["trading_day"],
                         "公告净值": pcf["nav"], "估值目标日": target},
            "篮子校验": {"公告NAV": pcf["nav"], "锚点NAV": anchor_nav,
                         "偏差bp": round(basket_diff_bp, 3)
                         if basket_diff_bp is not None else None},
            "PCF来源": {"来源": pcf.get("source"),
                        "公告交易日": pcf.get("trading_day"),
                        "抓取时间": pcf.get("_fetched_at"),
                        "快照滞后交易日": pcf.get("stale_days", 0)},
            "算法": {"id": ALGO_ID, "version": ALGO_VERSION},
        }
        return out

    # ══ 驱动 + ΔNAV ══
    d_nav, drv, comps_detail = 0.0, None, []
    if meta["kind"] == "treasury":
        # 每券市值权重（clean 取货币网成交，缺则 100）
        rows = []
        for b in bonds:
            key = _spot_key_for(b["name"], spot)
            q = spot.get(key) or {} if key else {}
            clean = _num(q.get("clean")) or 100.0
            dy = _num(q.get("dy_bp"))
            dur = _mod_dur(_num(q.get("yield")), _num(q.get("term")))
            v = b["qty"] * 10.0 * clean
            rows.append({"b": b, "key": key, "q": q, "clean": clean,
                         "dy": dy, "dur": dur, "v": v})
        v_sum = sum(r["v"] for r in rows) or 1.0
        hit = [r for r in rows if r["dy"] is not None and r["dur"]]
        if hit:
            # ① 活跃券 Δy（|dy| 最大 = 最新成交）× 面值加权组合久期
            act = max(hit, key=lambda r: abs(r["dy"]))
            d_port = sum(r["v"] * (r["dur"] or 0.0) for r in hit) \
                / sum(r["v"] for r in hit)
            dpct = -d_port * act["dy"] / 10000.0        # 小数（非百分数）
            # 逐券 carry（元/百元面值/日 → 每份）：Σ(v/100 × c × days) / unit
            d_carry = sum((r["v"] / 100.0) * (bpv.bond_daily_carry(r["q"]) or 0.0)
                          for r in hit) * days
            d_nav = anchor_nav * dpct + d_carry / unit
            drv = {"口径": "活跃券Δy×组合修正久期",
                   "活跃券": act["b"]["name"], "Δy_bp": act["dy"],
                   "组合修正久期": round(d_port, 3), "ΔP%": round(dpct * 100, 5),
                   "carry天数": days, "成份命中": "%d/%d" % (len(hit), len(rows))}
            out["method"] = "sse_pcf_treasury_dy"
            out["idx_chg"] = round(dpct * 100, 4)
            out["index_nm"] = "30年活跃券(参考)"
            out["idx_price"] = act["clean"]
        else:
            # ② TL 期货兜底
            tl_pct, tl_close, tl_day = _tl_pct()
            if tl_pct is not None:
                dpct = tl_pct / 100.0 * TL_RATIO           # 百分数 → 小数
                d_nav = anchor_nav * dpct
                drv = {"口径": "30年国债期货TL涨跌×%.2f" % TL_RATIO,
                       "合约": _tl_active(), "TL涨跌%": round(tl_pct, 4),
                       "K线日": tl_day}
                out["method"] = "sse_pcf_tl_proxy"
                out["idx_chg"] = round(tl_pct, 4)
                out["index_nm"] = "30年国债期货TL(参考)"
                out["idx_price"] = tl_close
            else:
                drv = {"口径": "纯票息（行情/期货均不可得）"}
                out["method"] = "sse_pcf_carry_only"
        comps_detail = [{"券": r["b"]["name"], "代码": r["b"]["id"],
                         "数量(千元面值)": r["b"]["qty"], "净价": r["clean"],
                         "Δy_bp": r["dy"], "修正久期": round(r["dur"], 3) if r["dur"] else None,
                         "市值权重%": round(r["v"] / v_sum * 100, 2)}
                        for r in rows]
    else:
        # ══ 信用债（511220）：银行间城投/中票代理 ══
        cs = _credit_stats(spot)
        if cs and cs.get("dy") is not None:
            bond_frac = (pcf["navpercu"] - (pcf["est_cash"] or 0.0)) / pcf["navpercu"]
            dpct = -(cs["term"] or 2.5) * cs["dy"] / 10000.0        # ΔP%（小数）
            carry_pct = (cs["yld"] or 1.9) / 365.0 / 100.0         # 年化%→日小数
            d_nav = anchor_nav * bond_frac * (dpct + carry_pct * days)
            drv = {"口径": "银行间%s Δy中位数 × 期限中位数（代理）"
                           % (cs.get("level") or "城投/中票"),
                   "代理Δy_bp": cs["dy"], "代理期限年": cs["term"],
                   "代理收益率%": cs["yld"], "样本数": cs["n"],
                   "样本稀薄": bool(cs.get("thin")),
                   "债券占比": round(bond_frac, 4), "carry天数": days}
            out["method"] = "sse_pcf_credit_dy"
            out["idx_chg"] = round(dpct * 100, 4)
            out["index_nm"] = "银行间城投债(代理)"
        else:
            drv = {"口径": "纯票息（信用代理行情不可得）"}
            out["method"] = "sse_pcf_carry_only"
        comps_detail = [{"成份券数": len(bonds), "合计数量": sum(b["qty"] for b in bonds),
                         "说明": "交易所城投债逐券行情无免费公开源，用银行间城投/中票作同向代理"}]

    est = anchor_nav + d_nav
    out["est"] = round(est, 4)
    out["detail"] = {
        "估值状态": "PCF 逐券估值（上交所公告）",
        "日期对齐": {"官方净值日": anchor_date, "官方净值": anchor_nav,
                     "行情日期": sp_date, "公告交易日": pcf["trading_day"],
                     "公告净值": pcf["nav"], "估值目标日": target,
                     "carry天数": days,
                     "说明": "公告 TradingDay=T 用 T-1 净值；行情日>净值日才叠加"},
        "篮子校验": {"公告NAV": pcf["nav"], "锚点NAV": anchor_nav,
                     "偏差bp": round(basket_diff_bp, 3)
                     if basket_diff_bp is not None else None,
                     "NAVperCU": pcf["navpercu"], "最小申赎单位": pcf["unit"],
                     "预估现金": pcf["est_cash"]},
        "PCF来源": {"来源": pcf.get("source"),
                    "公告交易日": pcf.get("trading_day"),
                    "抓取时间": pcf.get("_fetched_at"),
                    "快照滞后交易日": pcf.get("stale_days", 0)},
        "驱动变量": drv,
        "成份券": comps_detail if len(comps_detail) <= 12 else comps_detail[:12],
        "算法": {"id": ALGO_ID, "version": ALGO_VERSION,
                 "说明": "上交所 PCF（数量，无替代金额）+ 货币网现券/TL期货；"
                         "纯计算，锚定最新官方净值，不拟合不冻结"},
    }
    return out


if __name__ == "__main__":
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    for c in ("511090", "511130", "511220"):
        r = estimate_sse_bond_nav(c)
        print("==", c, r["method"], "est=", r["est"], "nav=", r["latest_nav"],
              r["nav_date"], "|", (r.get("note") or "")[:80])
        print("   驱动:", r["detail"].get("驱动变量"))
    print("-- 回测 2026-11-13（锚=11-12 净值）--")
    for c in ("511090", "511130", "511220"):
        r = estimate_sse_bond_nav(c, target_date="2026-11-13")
        hist = _nav_history(c, days=5)
        actual = dict(hist).get("2026-11-13")
        err = (r["est"] / actual - 1) * 10000 if (r["est"] and actual) else None
        print("==", c, r["method"], "est=", r["est"], "actual=", actual,
              "err_bp=", round(err, 2) if err else None)
