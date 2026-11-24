# -*- coding: utf-8 -*-
"""
backend/strategy_money_fund_alert.py —— 场内货币基金「溢价 / 买入赎回 / 申购卖出年化」钉钉提醒
============================================================================================

来源（2026-11-24 从 notice 迁入）
--------------------------------
原实现位于 notice/qdii_arbitrage_alert.py::MoneyFundQueryStrategy +
notice/crawl/trade_money_fund.py::trade_money_fund_jsl()。notice 侧已删除这两个文件。
迁入时做了两点改进：
  1. 取数改用纯 requests（etfarb 环境无 pandas/simplejson），集思录 URL 的
     硬编码时间戳改为动态生成（原 1761990217090 会随日期失效）。
  2. 每日一次推送 + 落盘去重（与 backend/strategy_cash_alert.py 一致），
     不再像原版每 50 分钟重复推同一批标的刷屏。

触发规则（沿用 notice 原阈值）
------------------------------
满足任一即入提醒（集思录口径，单位 %）：
  · 溢价           discount_rt_num >= 0.003%   （threshold_premium）
  · 买入赎回年化   profit_rt       >  1.5%     （threshold_annualized）
  · 申购卖出年化   profit_rt2      >  1.5%     （threshold_annualized）

数据源
------
集思录 T+0 货币基金列表：https://www.jisilu.cn/data/repo/trade_money_fund_list/
返回 JSON rows[].cell，字段映射：
  fund_id→代码  fund_nm→名称  discount_rt_num→溢价  profit_rt→买入赎回年化
  profit_rt2→申购卖出年化  t2_dt→到账日  unit_total→规模(亿)
匿名可取（无需 cookie）；失败返回空列表，本轮跳过、不误报。

钉钉推送
--------
复用 backend/dingtalk_utils.DingTalkNotifier.send_alert（内含 12:30–13:00 静默窗口）。
每个交易日只有首次出现触发信号时推送一次，状态落盘
log/strategy_money_fund_alert_state.json，进程重启不重复；发送失败/静默窗口不落标记，
下一轮继续重试。

用法
----
    python backend/strategy_money_fund_alert.py                # 常驻（每 5 分钟一轮）
    python backend/strategy_money_fund_alert.py --once --dry-run
    python backend/strategy_money_fund_alert.py --once --all-hours --force
"""
from __future__ import print_function

import argparse
import datetime
import json
import os
import sys
import time
import traceback

# ============ 路径（跨平台自适应，与 backend/app.py 同款） ============
_THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_THIS)
if _THIS not in sys.path:
    sys.path.insert(0, _THIS)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LOG_DIR = os.path.join(ROOT, "log")
LOG_FILE = os.path.join(LOG_DIR, "strategy_money_fund_alert.log")
STATE_FILE = os.path.join(LOG_DIR, "strategy_money_fund_alert_state.json")

# ============ 策略参数 ============
INTERVAL = 300                      # 轮询间隔（秒）= 5 分钟
TRADING_WINDOW = (9 * 60 + 15, 15 * 60 + 5)   # 工作日 09:15–15:05
TITLE = "场内货基套利提示"
THRESHOLD_PREMIUM = 0.003           # 溢价 >= 0.003%
THRESHOLD_ANNUALIZED = 1.5          # 买入赎回 / 申购卖出年化 > 1.5%
JSL_URL = "https://www.jisilu.cn/data/repo/trade_money_fund_list/"
REQUEST_TIMEOUT = 15

_NOTIFIER = None

# ============ 日志 ============
def _logf(msg):
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


# ============ 每日一次状态 ============
def _read_sent_date():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("sent_date")
    except Exception:
        return None


def _write_sent_date(today):
    """只在钉钉发送成功后调用；写失败仅记日志。"""
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


def _is_trading_session(now=None):
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return TRADING_WINDOW[0] <= hm <= TRADING_WINDOW[1]


def _notifier():
    global _NOTIFIER
    if _NOTIFIER is None:
        try:
            from dingtalk_utils import DingTalkNotifier
            _NOTIFIER = DingTalkNotifier()
        except Exception as e:
            _logf("钉钉通知器初始化失败: %s" % e)
            _NOTIFIER = False
    return _NOTIFIER or None


# ============ 取数（纯 requests，动态时间戳） ============
def fetch_rows():
    """拉取集思录全市场 T+0 货基，返回标准化 dict 列表；失败返回 []。"""
    try:
        import requests
        params = {"___jsl": "LST___t=%d" % int(time.time() * 1000)}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.post(JSL_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        payload = r.json()
        raw = payload.get("rows") or []
    except Exception as e:
        _logf("集思录货基取数失败：%s" % e)
        return []

    rows = []
    for item in raw:
        c = item.get("cell") or {}
        code = (c.get("fund_id") or "").strip()
        if not code:
            continue
        rows.append({
            "code": code,
            "name": (c.get("fund_nm") or "").strip(),
            "premium": _num(c.get("discount_rt_num")),        # 溢价 %
            "buy_redeem": _num(c.get("profit_rt")),           # 买入赎回年化 %
            "apply_sell": _num(c.get("profit_rt2")),          # 申购卖出年化 %
            "arrival": (c.get("t2_dt") or "").strip(),        # 到账日
            "scale": (c.get("unit_total") or "").strip(),     # 规模(亿)
        })
    return rows


# ============ 判定 ============
def judge(rows):
    """筛选触发标的，返回 (hits, reasons)；reasons: code -> 命中原因列表。"""
    hits, reasons = [], {}
    for it in rows:
        why = []
        if it["premium"] is not None and it["premium"] >= THRESHOLD_PREMIUM:
            why.append("溢价 %.4f%% ≥ %.4f%%" % (it["premium"], THRESHOLD_PREMIUM))
        if it["buy_redeem"] is not None and it["buy_redeem"] > THRESHOLD_ANNUALIZED:
            why.append("买入赎回年化 %.4f%% > %.1f%%" % (it["buy_redeem"], THRESHOLD_ANNUALIZED))
        if it["apply_sell"] is not None and it["apply_sell"] > THRESHOLD_ANNUALIZED:
            why.append("申购卖出年化 %.4f%% > %.1f%%" % (it["apply_sell"], THRESHOLD_ANNUALIZED))
        if why:
            hits.append(it)
            reasons[it["code"]] = why
    return hits, reasons


# ============ 消息拼装 ============
def build_rules_markdown():
    return (
        "\n\n#### 📌 策略规则\n\n"
        "- 触发（满足任一）：溢价 ≥ %.4f%%；买入赎回年化 > %.1f%%；申购卖出年化 > %.1f%%\n"
        "- 数据源：集思录 T+0 货币基金列表（匿名可取，每日动态取数）\n"
        "- 去重：每交易日首次触发推送一次，钉钉 12:30–13:00 静默窗口顺延\n"
        "- 提示：年化按集思录口径为估算值，实际以券商 APP 申赎确认与到账日为准\n"
        % (THRESHOLD_PREMIUM, THRESHOLD_ANNUALIZED, THRESHOLD_ANNUALIZED)
    )


def build_markdown(hits, reasons, source="集思录"):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "### %s" % TITLE,
        "",
        "> %s · 触发 **%d** 只 · 数据源 %s" % (now, len(hits), source),
        "",
        "| 代码 | 名称 | 溢价 | 买入赎回年化 | 申购卖出年化 | 到账日 | 规模(亿) |",
        "|---|---|---|---|---|---|---|",
    ]
    for it in hits:
        lines.append("| %s | %s | %s%% | %s%% | %s%% | %s | %s |" % (
            it["code"], it["name"], _fmt(it["premium"], 4),
            _fmt(it["buy_redeem"], 4), _fmt(it["apply_sell"], 4),
            it["arrival"] or "—", it["scale"] or "—"))
    lines.append("")
    lines.append("**触发原因**")
    lines.append("")
    for it in hits:
        lines.append("- %s %s：%s" % (it["code"], it["name"], "；".join(reasons[it["code"]])))
    lines.append(build_rules_markdown())
    return "\n".join(lines)


# ============ 单轮 ============
def run_once(dry_run=False, ignore_session=False, force=False):
    now = datetime.datetime.now()
    if not ignore_session and not _is_trading_session(now):
        return {"ok": True, "sent": False, "hits": 0, "reason": "非交易时段"}

    rows = fetch_rows()
    if not rows:
        return {"ok": True, "sent": False, "hits": 0, "reason": "取数为空/失败"}

    hits, reasons = judge(rows)
    if not hits:
        _logf("无触发（扫描 %d 只）" % len(rows))
        return {"ok": True, "sent": False, "hits": 0, "reason": "无触发"}

    today = now.strftime("%Y-%m-%d")
    if not force and _read_sent_date() == today:
        _logf("今日已推送，跳过（触发 %d 只）" % len(hits))
        return {"ok": True, "sent": False, "hits": len(hits), "reason": "今日已推送"}

    msg = build_markdown(hits, reasons)
    if dry_run:
        print("\n" + "=" * 70 + "\n" + msg + "\n" + "=" * 70 + "\n")
        return {"ok": True, "sent": False, "hits": len(hits), "reason": "dry-run",
                "items": hits}

    bot = _notifier()
    if not bot:
        return {"ok": False, "sent": False, "hits": len(hits), "reason": "通知器不可用"}

    ok = bot.send_alert(msg, TITLE)
    if ok:
        _write_sent_date(today)
        _logf("已发送钉钉：触发 %d 只（%s）" % (
            len(hits), "、".join("%s(%s)" % (h["code"], h["name"]) for h in hits[:10])))
    else:
        _logf("钉钉未发送（静默窗口或失败），触发 %d 只，下轮重试" % len(hits))
    return {"ok": True, "sent": bool(ok), "hits": len(hits),
            "reason": "已发送" if ok else "静默窗口/未发送"}


# ============ 常驻循环 ============
def run_forever(interval=INTERVAL, **kw):
    _logf("货基套利策略启动：间隔 %d 秒，溢价≥%.4f%% / 年化>%.1f%%"
          % (interval, THRESHOLD_PREMIUM, THRESHOLD_ANNUALIZED))
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
                          name="strategy_money_fund_alert", daemon=True)
    th.start()
    return th


def _bg_guard(interval, kw):
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

    ap = argparse.ArgumentParser(description="场内货基溢价/年化钉钉提醒")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息，不发钉钉")
    ap.add_argument("--interval", type=int, default=INTERVAL, help="轮询间隔秒（默认 300）")
    ap.add_argument("--all-hours", action="store_true", help="非交易时段也执行")
    ap.add_argument("--force", action="store_true", help="忽略每日一次限制，强制推送")
    a = ap.parse_args()

    kw = dict(dry_run=a.dry_run, ignore_session=a.all_hours, force=a.force)
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
