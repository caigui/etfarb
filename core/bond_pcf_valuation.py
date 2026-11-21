# -*- coding: utf-8 -*-
"""511520 政金债ETF富国 —— PCF 逐券实时估值 + 国开10Y活跃利率债参考。

本模块把 `fund_511520_realtime.py`（WorkBuddy 侧验证过的原型）的「方法二：
PCF 逐券价格法」移植进 ETFarb，替换 bond_etf_valuation 里 511520 原有的
`票息 + 10Y国债期货方向` 粗估口径。

════════════════════ 估值口径（方法二 / PCF 逐券法）════════════════════
1) 基数：富国官网每日申赎清单(PCF) 里的「替代金额」÷（数量×10）= 每只成份券
   的**全价**（含应计利息）。已用篮子恒等式校验：
       (Σ替代金额 + 预估现金部分) ÷ 最小申赎单位 = 官方净值   （偏差 <0.01bp）
2) 盘中变动：逐券叠加真实市场数据
       px_est = px_pcf × (1 + ΔP_i%) + carry_i/日 × 天数
   ΔP_i 来源优先级（逐券独立判断）：
       a) 上一交易日净价快照存在 → 真实净价变动（零推算，每日自动落盘）
       b) 组合基准 Δy（10Y 国开活跃券 bp）× 该券久期 —— 默认，曲线平行移动
       c) DY_MODE="per_bond" 时改用逐券成交 bp（旧口径，仅供回溯对比）
       d) 都没有 → 组合 Δy 兜底
   🔴 2026-11-14 实测：逐券成交 bp 对低流动性老券严重失真（24国开10 仅
       0.25bp、24农发20 0.31bp），加权 Δy 被拉到 2.59bp 而实际需要 3.48bp，
       当日少跌 7.8bp。故默认改用统一基准 Δy。
   🔴 carry 不再用固定 6.5 厘，而是**逐券反推票面利率**（2026-11-14）：
       由该券「成交净价 P + 到期收益率 y + 剩余期限 T」按年付息债券定价式解出
           C_i = (P − 100/(1+y)^T) / ((1 − (1+y)^−T)/y)      （票面利率 %）
           carry_i/日 = C_i / 365                              （元/百元面值）
       这样 1.6% 新券与 3.9% 老券各计各的息，组合 carry 由成份券结构决定，
       实测加权约 0.0063 元/日（旧固定值 0.0065 会系统性高估，估值偏多）。
       缺行情的券用组合加权 carry 兜底；全缺才回退 DAILY_ACCRUAL。
3) 仓位与汇总（2026-11-14）：
       债券 99.52% + 一日逆回购 0.48%，逆回购按 GC001 实时年化利率计息：
       NAV_basket = NAV(D-1) × [(Σ 数量×10×px_est + 预估现金) ÷ (Σ替代金额+预估现金)]
                    ← 只取篮子**相对变动**并锚定最新官方净值，消掉口径基差
       NAV_repo   = NAV(D-1) × (1 + r_repo × 天数/365)
       NAV_est    = 0.9952 × NAV_basket + 0.0048 × NAV_repo
   🔴 2026-11-14 东哥定稿：**不做任何拟合**。原公式末尾的 (1+校准bp/10000)
      与「盘后冻结官方净值」两条路径已默认关闭 —— 页面显示的必须是模型算出来的数，
      不是拿已公布净值回填、也不是用历史残差去贴答案（CALIB_APPLY / FREEZE_OFFICIAL_NAV）。

════════════════════ 日期对齐（2026-11-14 估值差归因）════════════════════
   PCF(D) 的替代金额对应 **D-1 收盘**（篮子恒等式 = 前一交易日净值）。
   若货币网行情日 ≤ 最新官方净值日，说明这批行情已被净值消化：
     · 价格效应必须归零（否则会把 11-13 的回溯估值冒充成当前估值）；
     · 但仍按「逐券票息 × 天数 + 逆回购计息」计算，输出计算值，
       **不再直接显示官方净值**（官方净值只作为锚点 nav0，不作为 est）。

════════════════════ 方向参考（仅展示，不参与估值）════════════════════
国开10Y活跃利率债（按货币网成交行情的剩余期限筛 8.3~10.6Y、取代码最新者）：
    · 指数价   = 该券当日成交净价
    · 指数涨幅 = 该券当日净价涨跌幅%（由 bp × 修正久期折算）
仅供人工对照，不参与任何估值计算。

════════════════════ 数据源 ════════════════════════
  · PCF 清单     : 富国官网 fundDetail 页面（服务端渲染，含成份券表）
  · 银行间现券   : 中国货币网 /ags/ms/cm-u-md-bond/CbtPri（需 curl_cffi 过 WAF）
  · 官方净值     : 天天基金 f10/lsjz
  · ETF 行情     : 新浪 hq.sinajs.cn

依赖：curl_cffi（货币网 TLS 指纹 + 富国官网均走它）；无 curl_cffi 时
     PCF/现券均不可用，本模块返回 est=None，调用方退化为最新净值。
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta

logger = logging.getLogger("bond_pcf_valuation")

try:
    from curl_cffi import requests as _curl
except Exception:                                  # 无 curl_cffi 时降级
    _curl = None

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "data", "bond_511520")

# ── 可调参数 ───────────────────────────────────────────────────────
DAILY_ACCRUAL = 0.0065      # 兜底值：全券都反推不出票息时才用（6.5厘/日/百元）
ACC_YEAR_DAYS = 365         # 应计利息年基数（银行间债券 ACT/365）
BOND_WEIGHT = 0.9952        # 债券仓位 99.52%
REPO_WEIGHT = round(1.0 - BOND_WEIGHT, 6)   # 一日逆回购仓位 0.48%
REPO_ANNUAL_DEFAULT = 0.015                 # GC001 取不到时的默认年化 1.5%
REPO_RATE_MIN, REPO_RATE_MAX = 0.003, 0.05  # 合理区间，防行情异常值
PCF_TTL = 1800              # PCF 清单缓存 30 分钟（每日只变一次）
SPOT_TTL = 30               # 货币网现券成交缓存 30 秒
REPO_TTL = 600              # GC001 行情缓存 10 分钟
ACTIVE_MIN_Y, ACTIVE_MAX_Y = 8.3, 10.6   # 国开10Y活跃券的剩余期限筛选窗口
PROBE_TIMEOUT = 6           # 首次探测「代理/直连」时每条链路的超时上限（秒）

# 🔴 2026-11-16 TLS 指纹降级（用户报「跑 start_dashboard.bat 后估值还是错的」根因）
#   现象：同一台机器、同一时刻
#     · 终端里手工跑 → 511520 est=119.1331（现券命中 15/17，真实净价）
#     · start_dashboard.bat 起的看板 → est=119.1822、method=pcf_basket_est、
#       note「现券行情不可得，逐券用活跃券Δy推算」、指数价「失败」
#   排查结论：不是代理问题（bat 清空 HTTP_PROXY 是对的），是 **TLS 指纹被拦**。
#   实测（连 www.chinamoney.com.cn，无代理直连，三轮复现一致）：
#     chrome / chrome120 / chrome131 / chrome136 / firefox133 → 全部 8~10s 超时
#     chrome110 / edge101 / safari / safari17_0              → 0.2~0.4s 正常
#   即 curl_cffi 的 `impersonate="chrome"` 会被分配到一个**新版本**指纹，
#   而这条网络链路上的设备（防火墙/网关）正好对新版 Chrome ClientHello 丢包。
#   bat 清空代理后必须直连，于是必然撞上；而终端继承 55159 代理时走代理反而绕过。
#   ⇒ 不能硬编码单一指纹，改成「按候选顺序逐个试，成功即锁定」。
TLS_IMPERSONATE_CANDIDATES = ("chrome110", "edge101", "safari", "chrome", "chrome120")
_IMP_OK = {"fp": None}      # 已锁定的指纹；None = 还没试出可用值


# 🔴 2026-11-14 估值差归因后新增（实测 11-13：模型 119.2517 vs 实际 119.1596）
#   1) 货币网 bp 是「逐券成交口径」的当日收益率变动，低流动性老券常常整天没成交，
#      bp 会滞留成失真值（24国开10 仅 0.25bp、24农发20 0.31bp），把组合加权 Δy
#      从基准 3.25bp 拉到 2.59bp → 当日少跌 7.8bp。政金债篮子以曲线平行移动为主，
#      故默认改用「10Y 活跃券 bp」作统一基准，逐券 bp 只留明细、不参与加权。
DY_MODE = "curve"           # "curve"=组合统一基准Δy(默认) / "per_bond"=旧逐券口径(回溯用)
#   2) 残差校准（历史机制，默认全关）：盘后净值公布时记录「模型隐含变动 vs 实际
#      净值变动」的偏差，EMA 平滑后用于盘中估值。
#      🔴 2026-11-14 东哥要求「不需要调整去拟合，根据计算来」：
#         CALIB_APPLY=False ⇒ 估值公式完全不乘校准系数，页面数值 100% 由模型算出；
#         CALIB_AUTO=False  ⇒ 连样本都不再写盘。两个开关只作为回溯/实验保留，
#         detail 里始终披露「未启用」，避免误以为数字被贴过答案。
CALIB_APPLY = False         # 总开关：是否把校准系数乘进估值（默认 False=纯计算）
CALIB_BP = 0.0              # 手动校准(bp)，非 0 时优先于自动校准
CALIB_AUTO = False          # 自动 EMA 校准开关（默认关闭，不再写 calib.json）
FREEZE_OFFICIAL_NAV = False  # 旧口径：无新增行情时直接返回官方净值（默认关闭）
CALIB_EMA_ALPHA = 0.4       # 平滑系数
CALIB_MAX_BP = 10.0         # |残差| 超过此值视为异常，不纳入也不使用
#   3) 每日现券净价快照落盘，次日起自动用「两日真实净价差」替代 Δy×久期推算（零推算）。
SPOT_SNAP_DIR = "spot_snapshots"

# ── 第三方估值源（2026-11-14 新增）───────────────────────────────────────
#   目的：压掉「货币网逐券成交口径 vs 基金第三方估值口径」的 ~2.9bp 结构差。
#   中国货币网公开两类数据，无需授权：
#     · ClsBndValExcel  逐券估值（全价/净价/收益率/修正久期/基点价值），
#       一次请求拿全量（国开 115 / 农发 85 / 进出口 59 只）。
#       ⚠️ 绝不能用 ClsBndVal 逐券轮询：连查约 10 次 WAF 就返回
#       "There are too many connections from your internet address"。
#     · ClsYldCurvHis   收盘收益率曲线，一次拿多日 × 15 个关键期限。
#   ⚠️ 这是 CFETS（交易中心）估值，不是中债金融估值中心的「中债估值」，两者
#      编制机构/样本/模型不同；但实测 511520 与其高度一致（11-13 复现误差 0.27bp）。
#   ⚠️ 收盘估值每日 17:00 后才发布，盘中拿不到当日 → 自动回退「曲线Δy × 真实久期」。
VAL_SOURCE = "cfets"      # "cfets"=逐券估值优先(默认) / "curve"=仅曲线Δy / "spot"=旧成交口径
ALGO_ID = "511520_pcf_basket_cfets_v2"       # 历史记录页展示的算法标识
ALGO_NAME = "PCF逐券篮子·成交净价/活跃券Δy·真实久期"
ALGO_VERSION = "2026-11-16b"   # b: 修复 bp 符号丢失 + 久期改用 CFETS 真实值 + 缓存键含 scale
CURVE_BOND_TYPE = "CYCC021"   # 政策性金融债(国开) 收盘到期收益率曲线
CURVE_TERM = 10.0             # 取 10Y 关键期限的日变动作组合统一 Δy
VAL_TTL = 1800            # 曲线/估值空结果缓存 30 分钟
_CFETS_BK = "https://www.chinamoney.com.cn/ags/ms/"
_CFETS_REF = "https://www.chinamoney.com.cn/chinese/bkval/"
_CURVE_REF = "https://www.chinamoney.com.cn/chinese/bkcurvclosedyhis/"
_CFETS_BTYPES = (("999901", "国开"), ("999902", "进出口"), ("999903", "农发"))
_cfets_cache = {}                 # 进程内：day -> {code: {...}}
_curve_cache = {"t": 0.0, "key": None, "v": None}
_val_state = {"failed": False}   # 只在状态切换时告警，避免看板刷屏

# 511520 成份券代码 → 久期（按发行年推算剩余期限 × 0.9，参考原型口径）
_DUR_CACHE = {}


# ════════════════════════════════ 工具 ════════════════════════════════
# 🔴 取数链路兜底（2026-11-13 实测踩坑，按顺序试，成功即锁定）：
#      · cfg   : 专用代理变量 BOND_511520_PROXY / 项目根目录 proxy.txt。
#                最高优先级 —— start_dashboard.bat 会主动清空 HTTP(S)_PROXY
#                （因为新浪/东财走 VPN 代理反而报 ProxyError），清空后环境变量
#                这条路就没了，但本机直连富国官网又会 timeout。留一个不被 bat
#                碰的专用入口，是唯一稳的解法。
#      · env   : 继承 HTTP(S)_PROXY（WorkBuddy / 部分 VPN 客户端会注入）。
#                ⚠️ 这类变量可能是**别的程序的临时端口**（换了就失效），所以它
#                只是候选之一，失败会继续往下试，不会被无条件信任。
#      · sys   : Windows 系统代理。两条子路径：
#                  sys        = ProxyEnable=1 时的 ProxyServer；
#                  sys_reg    = ProxyEnable=0 但注册表里仍留着 ProxyServer。
#                               常见情况：Clash/v2ray 装了但"系统代理"开关没开，
#                               注册表里的 127.0.0.1:10808 其实是活的，值得一试。
#      · direct: 显式禁用代理直连（少数能直连的机房/云主机）。
#
# 🔴 冷却策略（2026-11-13 第二次修）：
#   原先「全失败就冻结 300 秒」是错的 —— 网络恢复后要白等 5 分钟，而且期间每次
#   刷新都刷同一条 WARNING 噪音（用户实际报的就是这个）。
#   改为 **指数退避**：第 1 次失败退避 5s，之后 10/20/40，上限 60s。
#   退避到期后自动重试全部链路，网络一通立刻恢复，无需人工干预。
_NET_OK = {"mode": None}            # 已锁定的链路: None=未定 / cfg / env / sys / sys_reg / direct
_NET_FAIL = {"until": 0.0, "count": 0}   # 退避截止时间(monotonic) / 连续失败次数
_BACKOFF_BASE = 5.0                 # 首次退避 5 秒
_BACKOFF_MAX = 60.0                 # 退避上限 60 秒
_MODE_DESC = {"cfg": "专用代理变量 / proxy.txt",
              "env": "继承 HTTP(S)_PROXY 环境变量",
              "sys": "Windows 系统代理（已启用）",
              "sys_reg": "注册表里残留的系统代理（开关未开但端口可能可用）",
              "guess": "自动发现的本机代理端口",
              "direct": "显式禁用代理直连"}
PROBE_BUDGET = 10.0         # 探测阶段总预算（秒）：再多链路也不让首屏等超 10 秒
# 本机常见代理客户端的监听端口（Clash / v2ray / Shadowsocks / Fiddler / Burp…）
# 🔴 2026-11-13 实测：系统代理开关 ProxyEnable=0 时，注册表里的 127.0.0.1:10808
#    可能根本没在监听；反过来客户端在跑但没开系统代理时，注册表又什么都没写。
#    所以「扫一遍端口看谁真的在听」比读任何配置都可靠。
_PROXY_PORTS = (10808, 10809, 7890, 7897, 7891, 1080, 1087, 8888, 8889,
                2080, 8080, 8081, 20171, 20172, 4780, 6152, 6153)
_GUESS = {"url": None, "at": 0.0}     # 端口扫描结果缓存（url / 扫描时刻）
_GUESS_TTL = 60.0                      # 60 秒内不重复扫端口
# 专用代理变量：躲开 start_dashboard.bat 对 HTTP(S)_PROXY 的强制清空。
# 本机若「直连富国官网超时、但系统里有个可用代理」，二选一即可：
#   · 设环境变量 BOND_511520_PROXY=http://127.0.0.1:10808
#   · 或在项目根目录放一个 proxy.txt，里面只写一行代理地址
#     （推荐：不用改 bat，也不受控制台编码影响）
CFG_PROXY_ENV = "BOND_511520_PROXY"
CFG_PROXY_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "proxy.txt")


def _reg_proxy(allow_disabled=False):
    """读 Windows 注册表里的代理配置。

    allow_disabled=False → 仅在 ProxyEnable=1 时返回（即真正的「系统代理」）；
    allow_disabled=True  → 即使 ProxyEnable=0 也返回 ProxyServer
                           （客户端装了但开关没开，端口常常仍是活的）。
    非 Windows / 读不到 / 无 ProxyServer 时返回 None。
    """
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            try:
                enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
            except FileNotFoundError:
                enable = 0
            try:
                server, _ = winreg.QueryValueEx(k, "ProxyServer")
            except FileNotFoundError:
                server = ""
        finally:
            winreg.CloseKey(k)
        if not server or (not enable and not allow_disabled):
            return None
        server = str(server).strip()
        if "=" in server:            # 形如 "http=host:port;https=host:port"
            parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
            server = parts.get("https") or parts.get("http") or ""
        if not server:
            return None
        if "://" not in server:
            server = "http://" + server
        return server
    except Exception:
        return None


def _sys_proxy():
    """已启用的系统代理（ProxyEnable=1）。保留此函数名供外部/自检引用。"""
    return _reg_proxy(allow_disabled=False)


def _cfg_proxy():
    """读专用代理配置：环境变量优先，其次项目根目录 proxy.txt 的一行内容。"""
    u = (os.environ.get(CFG_PROXY_ENV) or "").strip()
    if u:
        return u
    try:
        with open(CFG_PROXY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except Exception:
        pass
    return None


def _backoff_left():
    """距离退避结束还有多少秒（0 = 现在就可以试）。"""
    return max(0.0, _NET_FAIL["until"] - time.monotonic())


def _port_alive(port, host="127.0.0.1", timeout=0.15):
    """裸 TCP connect 判断端口是否有人监听（比 bind 探测可靠得多）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _scan_ports(ports=None, timeout=0.15, workers=32):
    """并行探测多个端口，返回在监听的端口列表（按 _PROXY_PORTS 原顺序）。

    ⚠️ 必须并行：2026-11-13 实测串行扫 17 个端口要 4.36 秒 —— Windows 上
       连一个没人监听的本地端口并不立刻回 RST，而是等到超时。并行 ≈ 0.15 秒。
    """
    ports = list(ports or _PROXY_PORTS)
    alive = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(ports))) as ex:
            for p, ok in zip(ports, ex.map(lambda x: _port_alive(x, timeout=timeout), ports)):
                if ok:
                    alive[p] = True
    except Exception:
        alive = {p: True for p in ports if _port_alive(p, timeout=timeout)}
    return [p for p in ports if alive.get(p)]


def _guess_proxy():
    """扫本机常见端口，返回第一个「真的在监听」的代理地址。

    🔴 之所以需要这一步（2026-11-13）：这台机器上
        · 系统代理开关是关的（ProxyEnable=0），
        · 注册表残留的 10808 也没在监听，
        · 但代理客户端可能以别的端口在跑（Clash 7890 / v2ray 10808 / …）。
       光读配置永远判断不出来，只有 connect 一次才知道。
    结果缓存 60 秒，避免看板每刷一次都扫一遍端口。
    """
    now = time.monotonic()
    if now - _GUESS["at"] < _GUESS_TTL:
        return _GUESS["url"]
    found = None
    live = _scan_ports()
    if live:
        found = "http://127.0.0.1:%d" % live[0]
        logger.debug("[511520] 自动发现本机代理端口: %s", live)
    _GUESS["url"], _GUESS["at"] = found, now
    return found


def _net_modes():
    """返回本轮要尝试的链路顺序（空 tuple = 仍在退避窗口内）。"""
    if _NET_OK["mode"]:
        return (_NET_OK["mode"],)
    if _backoff_left() > 0:
        return ()
    modes = []
    if _cfg_proxy():
        modes.append("cfg")
    if any(os.environ.get(k) for k in
           ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")):
        # 只在真的配了代理变量时才试 env；start_dashboard.bat 会主动清空它们，
        # 清了之后「env」与「direct」对 libcurl 是同一件事（都是无代理），
        # 重复试只会白等一整轮 PROBE_TIMEOUT。
        modes.append("env")
    if _reg_proxy(allow_disabled=False):
        modes.append("sys")
    elif _reg_proxy(allow_disabled=True):
        # 开关没开但注册表有地址 → 当成一个候选（常见的 Clash 未开系统代理场景）
        modes.append("sys_reg")
    if _guess_proxy():
        # 配置全不靠谱时的最后一张牌：谁在监听就试谁
        modes.append("guess")
    modes.append("direct")
    return tuple(modes)


def _net_kw(mode, kw):
    if mode == "direct":
        # 🔴 curl_cffi/libcurl 的坑（2026-11-13 实测）：proxies 里给 None 或干脆不传，
        #    libcurl 仍会读 HTTP(S)_PROXY 环境变量；必须给**空字符串**才是真正的
        #    「禁用该协议代理」。传 None 的写法在坏代理环境下无效，会继续卡住。
        kw["proxies"] = {"http": "", "https": ""}
    elif mode == "cfg":
        u = _cfg_proxy()
        if not u:
            raise RuntimeError("未配置 %s / proxy.txt" % CFG_PROXY_ENV)
        if "://" not in u:
            u = "http://" + u
        kw["proxies"] = {"http": u, "https": u}
    elif mode in ("sys", "sys_reg"):
        u = _reg_proxy(allow_disabled=(mode == "sys_reg"))
        if not u:
            raise RuntimeError("未读到系统代理")
        kw["proxies"] = {"http": u, "https": u}
    elif mode == "guess":
        u = _guess_proxy()
        if not u:
            raise RuntimeError("未发现本机代理端口")
        kw["proxies"] = {"http": u, "https": u}
    return kw


def _fp_order(prefer=None):
    """本轮要试的 TLS 指纹顺序：已锁定的排最前，其余按候选表依次兜底。"""
    locked = _IMP_OK["fp"]
    if locked:
        return (locked,)
    order = []
    if prefer and prefer in TLS_IMPERSONATE_CANDIDATES:
        order.append(prefer)
    order += [f for f in TLS_IMPERSONATE_CANDIDATES if f != prefer]
    return tuple(order)


def _req(method, url, timeout=20, **kw):
    """curl_cffi 请求，自动在「专用变量 / 环境代理 / 系统代理 / 直连」间兜底，
    并在每次链路尝试内部再按 TLS 指纹候选逐个降级。

    ⚠️ 探测中（未锁定链路）时单次超时压到 PROBE_TIMEOUT，避免坏代理把看板首屏
       拖到几十秒；锁定后可用长超时。
    """
    if _curl is None:
        raise RuntimeError("缺少 curl_cffi，无法访问 %s" % url)
    modes = _net_modes()
    if not modes:
        raise RuntimeError(
            "取数链路全部不可用，%.0f 秒后自动重试（本地快照兜底中）" % _backoff_left())
    kw = dict(kw)
    _prefer_fp = kw.pop("impersonate", None)
    errs = []
    t_start = time.monotonic()
    for mode in modes:
        probing = _NET_OK["mode"] is None
        for fp in _fp_order(_prefer_fp):
            if probing:
                # 探测阶段不能把看板首屏拖死：总预算 PROBE_BUDGET 秒，多条链路分着用
                left = PROBE_BUDGET - (time.monotonic() - t_start)
                if left <= 0.5:
                    errs.append("%s/%s: 跳过（探测预算已用完）" % (mode, fp))
                    continue
                to = min(timeout, PROBE_TIMEOUT, left)
            else:
                to = timeout
            try:
                r = getattr(_curl, method)(url, timeout=to, impersonate=fp,
                                           **_net_kw(mode, dict(kw)))
                if _NET_OK["mode"] != mode:
                    logger.info("[511520] 取数链路确定: %s（%s）",
                                mode, _MODE_DESC.get(mode, mode))
                    _NET_OK["mode"] = mode
                if _IMP_OK["fp"] != fp:
                    # 首次试出可用指纹时记一条：这是「为什么终端能跑、看板不能跑」的答案
                    logger.info("[511520] TLS 指纹确定: %s（%s）", fp,
                                "候选表首个可用值" if _IMP_OK["fp"] is None else "链路已切换")
                    _IMP_OK["fp"] = fp
                if _NET_FAIL["count"]:
                    logger.info("[511520] 取数链路已恢复（此前连续失败 %d 次）",
                                _NET_FAIL["count"])
                    _NET_FAIL.update(until=0.0, count=0)
                return r
            except Exception as e:
                msg = str(e)
                errs.append("%s/%s: %s" % (mode, fp, msg))
                # 指纹类失败（超时/连接被断）降为 debug，不刷屏；其余仍留痕
                logger.debug("[511520] 取数方式 %s/%s 失败: %s", mode, fp, msg)
                # 已被指纹拦死的链路不必再换指纹重试整轮：只有「连接超时」
                # 才值得换指纹再试一次（其它错误换个指纹也一样）
                if "timed out" not in msg and "Connection timed out" not in msg:
                    break
        if mode == "guess":
            # 扫出来的端口可能下一秒就没了，失败即作废旧缓存，下一轮重扫
            _GUESS.update(url=None, at=0.0)
    # 全链路失败 → 指数退避（不是固定 300s 冻结）
    _NET_FAIL["count"] += 1
    wait = min(_BACKOFF_BASE * (2 ** (_NET_FAIL["count"] - 1)), _BACKOFF_MAX)
    _NET_FAIL["until"] = time.monotonic() + wait
    raise RuntimeError("取数链路全部失败（第 %d 次），%.0f 秒后自动重试 -> %s"
                       % (_NET_FAIL["count"], wait, " | ".join(errs)))


def _get(url, referer=None, timeout=20, impersonate=None):
    """GET；货币网/富国官网均需浏览器 TLS 指纹。

    impersonate=None ⇒ 由 _req 按 TLS_IMPERSONATE_CANDIDATES 自动降级选定
    （2026-11-16：硬编码 "chrome" 在该网络链路会被丢包，详见常量区注释）。
    """
    h = {"User-Agent": _UA}
    if referer:
        h["Referer"] = referer
    return _req("get", url, timeout=timeout, headers=h,
                impersonate=impersonate).text


def _post(url, data, referer, timeout=20):
    return _req("post", url, timeout=timeout, data=data,
                headers={"User-Agent": _UA, "Referer": referer,
                         "X-Requested-With": "XMLHttpRequest"},
                impersonate=None).json()


def estimate_duration(code, scale=0.9):
    """按债券代码前两位（发行年）推算剩余期限，× scale 作为修正久期近似。

    🔴 2026-11-16 修复缓存污染：早期版本用 `code` 单键缓存，但同一只券会被
       不同 scale 调用（组合久期用默认 0.9，活跃券/单券用 1.0）。先调 0.9 后调 1.0
       就会把 1.0 的值写回缓存，之后所有默认调用都拿到被放大的久期。
       实测：260210 先 0.9→9.00，再 1.0→10.00，随后默认调用返回 10.00（应 9.00），
       组合久期因此被高估约 5.5%。改为以 (code, scale) 为键。
    """
    key = (str(code), round(float(scale), 4))
    if key in _DUR_CACHE:
        return _DUR_CACHE[key]
    y = date.today().year
    try:
        issue_year = 2000 + int(str(code)[:2])
    except (ValueError, TypeError):
        return 7.5
    age = y - issue_year
    remain = 10.0 if age <= 0 else (9.5 if age >= 8 else max(1.0, 10.0 - age))
    dur = round(remain * scale, 2)
    _DUR_CACHE[key] = dur
    return dur


# ══════════════ 逐券应计利息（carry）：由行情反推票面利率 ══════════════
def implied_coupon_pct(clean, ytm_pct, term_y):
    """由「成交净价 / 到期收益率 / 剩余期限」反推票面利率(%)。

    年付息、面值 100 的定价式：
        P = C × (1 − (1+y)^−T)/y + 100 × (1+y)^−T
    解出年票息 C（元/百元面值，数值上 = 票面利率%）：
        C = (P − 100 × (1+y)^−T) / ((1 − (1+y)^−T)/y)

    为什么这样做（2026-11-14）：PCF 清单只有「替代金额」即全价，货币网成交
    接口也不返回票息字段，外部逐券票息表需要额外数据源且换券就要维护。而
    净价 / 收益率 / 待偿期三个字段现成就有，反推出的票息随成份券结构自动更新，
    新券(1.6~1.9%)与老券(3.8~3.9%)各计各的息，组合 carry 不再是一个拍脑袋定值。

    T 用小数年做连续近似（不逐笔拆付息日），对估值精度影响 <0.01bp。
    参数异常或解出明显不合理（<0 或 >10%）时返回 None。
    """
    try:
        P, y, T = float(clean), float(ytm_pct) / 100.0, float(term_y)
    except (TypeError, ValueError):
        return None
    if P <= 50 or P >= 160 or y <= 0 or y >= 0.15 or T <= 0.3 or T > 40:
        return None
    df = (1.0 + y) ** (-T)
    annuity = (1.0 - df) / y
    if annuity <= 0:
        return None
    c = (P - 100.0 * df) / annuity
    return c if 0 < c < 10.0 else None


def bond_daily_carry(spot_i, fallback=None):
    """单券每日应计利息（元/百元面值/日），spot_i 为 fetch_spot_deal 的一项。"""
    c = implied_coupon_pct((spot_i or {}).get("clean"),
                           (spot_i or {}).get("yield"),
                           (spot_i or {}).get("term"))
    if c is None:
        return fallback
    return c / ACC_YEAR_DAYS


def portfolio_daily_carry(bonds, spot):
    """按成份券数量权重算组合加权日 carry（元/百元/日）。

    返回 (加权carry, {券简称: 逐券carry}, 命中数)。无法反推的券不参与加权，
    其权重由命中券重新归一化；全不命中则回退 DAILY_ACCRUAL。
    """
    hit = {}
    for b in bonds:
        if b.get("qty", 0) <= 0:
            continue
        c = bond_daily_carry((spot or {}).get(b.get("name")))
        if c is not None:
            hit[b["name"]] = c
    wsum = sum(b["qty"] for b in bonds
               if b.get("qty", 0) > 0 and b["name"] in hit)
    if wsum > 0:
        avg = sum(b["qty"] * hit[b["name"]] for b in bonds
                  if b.get("qty", 0) > 0 and b["name"] in hit) / wsum
        return avg, hit, len(hit)
    return DAILY_ACCRUAL, {}, 0


_static_carry_cache = {"t": 0.0, "unit": None, "src": None}


def static_carry_unit(force=False):
    """组合日 carry 折算成「元/份净值/日」，供历史静态估值使用。

        carry_元每份 = 加权日carry(元/百元) × Σ(数量×10) ÷ 最小申赎单位

    历史核对页逐日循环会跑上百行，这里带 30 分钟缓存（结构一天才变一次），
    避免因循环耗时触发重复取现券行情。返回 (元/份/日, 口径说明)。
    """
    now = time.monotonic()
    if not force and _static_carry_cache["unit"] is not None \
            and now - _static_carry_cache["t"] < 1800:
        return _static_carry_cache["unit"], _static_carry_cache["src"]
    pcf = load_pcf_local("511520")
    bonds = [b for b in (pcf or {}).get("bonds", []) if b.get("qty", 0) > 0]
    spot = None
    try:
        spot = fetch_spot_deal()
    except Exception:
        pass
    avg, _, n = portfolio_daily_carry(bonds, spot) if bonds else (
        DAILY_ACCRUAL, {}, 0)
    face = sum(b["qty"] * 10 for b in bonds)
    unit = (pcf or {}).get("min_unit") or 1000.0
    val = avg * face / unit if unit > 0 else DAILY_ACCRUAL
    src = ("逐券反推票息×篮子面值/申赎单位（命中%d/%d只）" % (n, len(bonds))
           if n else "行情缺失，回退兜底固定票息")
    _static_carry_cache.update(t=now, unit=val, src=src)
    return val, src


# ══════════════ 一日逆回购（GC001）利率 ══════════════
_repo_cache = {"t": 0.0, "rate": None, "src": None}


def fetch_repo_rate(force=False):
    """GC001（204001）最新年化利率，返回 (小数年化, 来源)。

    腾讯行情 v_sh204001 字段[3] 即当日回购年化利率（数值如 1.265 表示 1.265%）。
    取不到 / 值不合理时回退 REPO_ANNUAL_DEFAULT，保证估值链路永不断。
    """
    now = time.monotonic()
    if not force and _repo_cache["rate"] is not None \
            and now - _repo_cache["t"] < REPO_TTL:
        return _repo_cache["rate"], _repo_cache["src"]
    rate, src = None, None
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(
            "https://qt.gtimg.cn/q=sh204001",
            headers={"User-Agent": _UA, "Referer": "https://gu.qq.com/"})
        raw = urllib.request.urlopen(req, timeout=8, context=ctx).read().decode("gbk", "ignore")
        m = re.search(r'v_sh204001="([^"]+)"', raw)
        if m:
            v = float(m.group(1).split("~")[3]) / 100.0
            if REPO_RATE_MIN <= v <= REPO_RATE_MAX:
                rate, src = v, "GC001实时"
    except Exception as e:
        logger.debug("[511520] GC001 行情不可用，用默认逆回购利率: %s", e)
    if rate is None:
        rate, src = REPO_ANNUAL_DEFAULT, "默认年化%.2f%%" % (REPO_ANNUAL_DEFAULT * 100)
    _repo_cache.update(t=now, rate=rate, src=src)
    return rate, src


def _trading_days_between(d0, d1, holidays=None, makeup=None):
    """d0 → d1 的交易日数（含端点外推，用于应计天数；简化按自然日间扣除周末）。"""
    try:
        a = datetime.strptime(d0, "%Y-%m-%d").date()
        b = datetime.strptime(d1, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 1
    n = (b - a).days
    return max(n, 0)


# ════════════════════════════ PCF 清单（富国官网）════════════════════════════
_pcf_cache = {"t": 0.0, "v": None}
_pcf_lock = threading.Lock()
_pcf_state = {"stale": False}        # 当前是否在用本地快照（仅用于「变化时才告警」的去重）


def _pcf_fallback(code, reason, hint="网络不可达"):
    """官网 PCF 取不到时统一回退本地最近快照，并标注真实原因。

    hint 用于区分「网络不通」与「当日尚未发布」——两者都会走旧数据，但给用户
    的排查指引完全不同，不能混成一句"官网抓取失败"。
    """
    cached = load_pcf_local(code)
    if not (cached and cached.get("bonds")):
        raise RuntimeError("官网 PCF 不可用且无本地快照: %s" % reason)
    # 🔴 只在「在线→回退」切换点告警一次：看板 30 秒一刷，每次 warning 会刷满屏
    if not _pcf_state["stale"]:
        logger.warning("[511520] 官网 PCF 不可用（%s），改用本地快照 %s（原因: %s）；"
                       "条件恢复后会自动切回在线", hint, cached.get("date"), reason)
        _pcf_state["stale"] = True
    else:
        logger.debug("[511520] PCF 仍用本地快照 %s: %s", cached.get("date"), reason)
    out = dict(cached)
    out["source"] = "本地上次快照(%s)" % hint
    out["stale"] = True
    with _pcf_lock:
        _pcf_cache["t"] = time.monotonic()
        _pcf_cache["v"] = out
    return out


def fetch_pcf_online(code="511520", force=False, retries=2):
    """抓富国官网 PCF：返回
    {date, nav, min_unit, cash, bonds:[{code,name,qty,amount}], source}

    网络偶发失败（超时/5xx）时按 retries 重试；仍失败则回退到本地最近一份
    落盘快照 —— 宁可给出「T-1 的 PCF」并标注来源，也不静默退回旧估值口径。
    """
    now = time.monotonic()
    with _pcf_lock:
        if not force and _pcf_cache["v"]:
            # 在线结果可以缓存 30 分钟；降级快照只缓存 60 秒 —— 官网一旦发布
            # 当日 PCF 要能尽快切回，但也不能让看板每 30 秒刷新都重抓两遍官网。
            _ttl = 60 if _pcf_cache["v"].get("stale") else PCF_TTL
            if now - _pcf_cache["t"] < _ttl:
                return _pcf_cache["v"]

    url = "https://www.fullgoal.com.cn/fundDetail/%s/index.html" % code
    html = None
    last_err = None
    for attempt in range(max(1, retries)):
        try:
            html = _get(url, referer="https://www.fullgoal.com.cn/")
            if html and "成份股信息内容" in html:
                break
            last_err = RuntimeError("页面未含『成份股信息内容』（疑似限流或改版）")
            html = None
        except Exception as e:
            last_err = e
        if attempt + 1 < max(1, retries):
            time.sleep(1.0 + attempt)
    if html is None:
        return _pcf_fallback(code, last_err, "网络不可达")

    def grab(pat, default=None):
        m = re.search(pat, html, re.S)
        return m.group(1).strip() if m else default

    anchor = html.find("成份股信息内容")
    head = html[:anchor] if anchor > 0 else html
    dates = re.findall(r"(\d{4}-\d{2}-\d{2})日信息内容", head)
    pcf_date = dates[-1] if dates else None
    nav = grab(r"基金份额净值\(单位:元\)：\s*</td><td[^>]*>([\d\.]+)")
    min_unit = grab(r"最小申购、赎回单位\(单位:份\)：\s*</td><td[^>]*>(\d+)")
    cash = grab(r"预估现金部分\(单位:元\)：\s*</td><td[^>]*>(-?[\d\.]+)")

    bonds = []
    for m in re.finditer(
            r"<tr><td>(\d{5,6})</td><td>([^<]*)</td><td>(\d+)</td>"
            r"<td>[^<]*</td><td>[^<]*</td><td>[^<]*</td><td>(-?[\d\.]+)</td>", html):
        try:
            bonds.append({"code": m.group(1), "name": m.group(2).strip(),
                          "qty": float(m.group(3)), "amount": float(m.group(4))})
        except ValueError:
            continue
    if not bonds:
        # 🔴 2026-11-14 实测：周末/未披露日，官网会返回一个**字段全是 "--"、
        #    成份券表只有表头没有数据行**的空模板。这不是改版、更不是网络故障，
        #    原来直接抛异常会让整条 511520 估值链断掉。按「当日尚未发布」回退快照。
        return _pcf_fallback(code,
                             RuntimeError("当日 PCF 尚未发布（页面为空模板）"),
                             "当日PCF尚未发布")

    out = {"date": pcf_date or date.today().strftime("%Y-%m-%d"),
           "nav": float(nav) if nav else None,
           "min_unit": float(min_unit) if min_unit else 1000.0,
           "cash": float(cash) if cash else 0.0,
           "bonds": bonds, "source": "富国官网"}
    if _pcf_state["stale"]:
        logger.info("[511520] 官网 PCF 已恢复在线抓取（%s，%d 只成份券）",
                    out["date"], len(bonds))
        _pcf_state["stale"] = False
    # 篮子校验：Σ替代金额+预估现金 ÷ 最小申赎单位 应等于官方净值
    if out["nav"] and out["min_unit"]:
        implied = (sum(b["amount"] for b in bonds) + out["cash"]) / out["min_unit"]
        out["basket_implied"] = round(implied, 6)
        out["basket_diff_bp"] = (implied / out["nav"] - 1) * 10000
        if abs(out["basket_diff_bp"]) > 5:
            logger.warning("[511520] PCF 篮子校验偏差 %.2fbp（隐含 %.4f vs 官方 %.4f），"
                           "疑似清单与净值不同日", out["basket_diff_bp"], implied, out["nav"])
    with _pcf_lock:
        _pcf_cache["t"] = now
        _pcf_cache["v"] = out
    _save_pcf(out, code)
    return out


def _pcf_file(code, d):
    return os.path.join(DATA_DIR, "pcf_%s_%s.json" % (code, d.replace("-", "")))


def _save_pcf(pcf, code="511520"):
    """按日期落盘（便于历史回溯与方法二的「两日净价变动」对比）。

    同时附带当日「国开10Y活跃券」快照 —— 供历史核对页在次日及以后
    用真实活跃券净价替代国债期货代理，无需依赖任何历史行情接口。
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        rec = dict(pcf)
        act = pick_active_cdb10y()
        if act:
            rec["active_cdb10y"] = {
                "code": act["code"], "name": act["name"],
                "clean": act["clean"], "yield": act["yield"],
                "dy_bp": act["dy_bp"], "term": act["term"],
                "show_time": act.get("show_time"),
            }
        with open(_pcf_file(code, pcf["date"]), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
    except Exception as e:
        logger.debug("[511520] PCF 落盘失败: %s", e)


def load_pcf_series(code="511520"):
    """读本地全部 PCF 快照 {date: pcf}（升序），用于历史核对页逐日对比。"""
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


def static_val_from_pcf(code="511520"):
    """用本地逐日 PCF 快照算「静态估值」历史 {date: {...}}。

    日期对齐（关键，容易错位一天）：
        富国官网 PCF(D) 披露的 nav 是 D-1 的官方净值，bonds 的替代金额
        对应 D-1 收盘估值。实测：PCF(2026-11-13).nav = 119.4976 = 11-12 官方净值。

    所以相邻两份 PCF(X-1)、PCF(X)：
        v(X-1)/v(X-2) = X-1 的全价变动比例
        static_val(X-1) = PCF(X-1).nav × v(X-1)/v(X-2)
                        = 净值(X-2) × 变动(X-1) = 预测净值(X-1)
    即结果归属 **p0（前一份）的日期**，不是 p1 的日期。

    完全由基金公司公布的实际全价变化驱动，零推算。日期不足两天则不出行。
    """
    series = load_pcf_series(code)
    dates = sorted(series)
    out = {}
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        p0, p1 = series[d0], series[d1]
        v0 = sum(b["qty"] * 10 * (b["amount"] / (b["qty"] * 10))
                 for b in p0["bonds"] if b["qty"] > 0)
        v1 = sum(b["qty"] * 10 * (b["amount"] / (b["qty"] * 10))
                 for b in p1["bonds"] if b["qty"] > 0)
        if v0 <= 0 or v1 <= 0 or not p0.get("nav"):
            continue
        act = p1.get("active_cdb10y") or {}
        out[d0] = {
            "static_val": round(p0["nav"] * (v1 / v0), 4),
            "dir_src": "pcf_real",
            "active_code": act.get("code"),
            "active_price": act.get("clean"),
            "active_pct": (round(-((_real_duration(act["code"])
                                    or estimate_duration(act["code"], 1.0))
                                   / 100.0) * (act.get("dy_bp") or 0), 4)
                           if act.get("clean") else None),
        }
    return out


def load_pcf_local(code="511520", d=None):
    """读本地已落盘的指定日期 PCF；d=None 取最近一份（排除 given_date）。"""
    if not os.path.isdir(DATA_DIR):
        return None
    files = [f for f in os.listdir(DATA_DIR)
             if f.startswith("pcf_%s_" % code) and f.endswith(".json")]
    if not files:
        return None
    if d:
        fn = "pcf_%s_%s.json" % (code, d.replace("-", ""))
        if fn not in files:
            return None
    else:
        fn = sorted(files)[-1]
    try:
        with open(os.path.join(DATA_DIR, fn), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_prev_pcf(code="511520", today=None):
    """取 today 之前最近的一份本地 PCF（用于净价变动对比）。"""
    if not os.path.isdir(DATA_DIR):
        return None
    today8 = (today or date.today().strftime("%Y-%m-%d")).replace("-", "")
    cands = sorted(f for f in os.listdir(DATA_DIR)
                   if f.startswith("pcf_%s_" % code) and f.endswith(".json")
                   and f.split("_")[-1][:8] < today8)
    if not cands:
        return None
    try:
        with open(os.path.join(DATA_DIR, cands[-1]), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ════════════════════════════ 货币网现券成交行情 ════════════════════════════
_spot_cache = {"t": 0.0, "v": None}
_spot_lock = threading.Lock()
_spot_state = {"failed": False}      # 现券取数状态：仅用于「变化时才告警」的去重

_CM_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-md-bond/CbtPri"
_CM_REF = "https://www.chinamoney.com.cn/chinese/mkdatabond/"


def fetch_spot_deal(force=False):
    """中国货币网银行间现券成交行情 {简称: {...}}。

    字段：clean（成交净价）/ yield（最新收益率%）/ dy_bp（当日收益率变动bp）
          / code / term（剩余期限年）/ show_time
    注意：bp 是**收益率变动(bp)**，不是价格变动。

    🔴 日志策略（2026-11-13）：只在「正常→失败」和「失败→恢复」两个切换点各告警
       一次，连续失败降为 debug。原先是每次调用都 logger.warning，而看板 30 秒
       一刷，网络不通时会刷出一整屏重复 WARNING，真正有用的信息被淹没。
    """
    now = time.monotonic()
    with _spot_lock:
        if not force and _spot_cache["v"] and now - _spot_cache["t"] < SPOT_TTL:
            return _spot_cache["v"]
    try:
        j = _post(_CM_URL, {"flag": "1", "lang": "cn", "bondName": ""}, _CM_REF)
        recs = (j.get("records") or [])
        _spot_state["failed"] = False
    except Exception as e:
        if not _spot_state["failed"]:
            # 首次失败才告警。若本轮的根因是「整条取数链路不可用」，PCF 那边
            # 已经打过一条说明同一件事的 WARNING 了 —— 同一根因刷两条纯属噪音，
            # 这里降为 info（仍留档），只有「链路通但货币网单独挂」才 warning。
            _same_root = "取数链路全部" in str(e)
            if _same_root:
                logger.info("[511520] 现券成交随链路一起不可用: %s", e)
            else:
                logger.warning("[511520] 货币网现券成交获取失败（将自动重试）: %s", e)
            _spot_state["failed"] = True
        else:
            logger.debug("[511520] 货币网现券成交仍不可用: %s", e)
        recs = []
    out = {}
    for r in recs:
        nm = (r.get("abdAssetEncdShrtDesc") or "").strip()
        if not nm:
            continue
        def _f(k):
            try:
                return float(r.get(k))
            except (TypeError, ValueError):
                return None
        tm = r.get("termToMaturity") or ""
        m = re.match(r"([\d\.]+)Y", tm)
        clean_px = _f("dmiLatestRate")
        ytm = _f("dmiLatestContraRate")
        # 🔴 2026-11-16 关键修复：`bp` 是**幅度**不是带符号的变动
        #   实测 1543 条记录里 bp < 0 的一条都没有（0.0%），而 bpNum < 0 有 24.5%。
        #   逐券核对：15 只里 12 只 bp 与 bpNum 同号、3 只（25国开10/24农发10/16国开05）
        #   bp 为正而 bpNum 为负 —— 正好对应「净价上涨、收益率下行」。
        #   所以：bpNum 才是带符号的真实收益率变动；bp 只在 bpNum 缺失时作幅度参考。
        #   旧代码直接取 bp，导致「跌」的信号永远为正、方向恒为跌，指数涨幅
        #   显示 -0.47% 而实际在涨（活跃券真实 +0.21%），这是本次问题的根因。
        dy_signed = _f("bpNum")
        if dy_signed is None:
            dy_signed = _f("bp")      # 缺 bpNum 时退化为幅度（方向未知，仅兜底）
        out[nm] = {
            "code": r.get("bondcode"),
            "clean": clean_px,
            "yield": ytm,
            "dy_bp": dy_signed,
            "bp_abs": _f("bp"),       # 货币网原始幅度字段，留档核对
            "term": float(m.group(1)) if m else None,
            "show_time": r.get("showDate"),
        }
    if out:
        with _spot_lock:
            _spot_cache["t"] = now
            _spot_cache["v"] = out
        # 当日净价落盘：次日自动用「两日真实净价差」估值，不再靠 Δy×久期推算
        _save_spot_snapshot(out)
        if _spot_state["failed"]:
            logger.info("[511520] 货币网现券成交已恢复（%d 只）", len(out))
            _spot_state["failed"] = False
    elif recs:
        logger.warning("[511520] 货币网返回 %d 条记录但未解析出可用现券（字段可能改版）",
                       len(recs))
    return out


def _latest_spot_date(spot):
    """从 show_time 里取最新行情日期 YYYY-MM-DD（无则 None）。"""
    ds = []
    for v in (spot or {}).values():
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(v.get("show_time") or ""))
        if m:
            ds.append(m.group(1))
    return max(ds) if ds else None


def _save_spot_snapshot(spot):
    """把当日现券净价落盘到 data/bond_511520/spot_snapshots/spot_YYYYMMDD.json。

    只存净价/收益率/期限，一天一份、同名覆盖，不会无限增长。
    次日 load_spot_prev() 读出后即可走「净价实际变动」分支（零推算）。
    """
    d = _latest_spot_date(spot)
    if not d:
        return
    try:
        folder = os.path.join(DATA_DIR, SPOT_SNAP_DIR)
        os.makedirs(folder, exist_ok=True)
        slim = {nm: {"clean": v.get("clean"), "yield": v.get("yield"),
                     "bp": v.get("dy_bp"), "term": v.get("term")}
                for nm, v in (spot or {}).items()
                if v.get("clean") and v["clean"] > 0}
        fp = os.path.join(folder, "spot_%s.json" % d.replace("-", ""))
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({"date": d, "spot": slim}, f, ensure_ascii=False)
    except Exception as e:
        logger.debug("[511520] 现券净价快照落盘失败: %s", e)


def load_spot_prev(before=None):
    """加载日期早于 before 的最近一份净价快照 {简称: 净价}。"""
    try:
        import glob
        fs = sorted(glob.glob(os.path.join(DATA_DIR, SPOT_SNAP_DIR, "spot_*.json")))
        for fp in reversed(fs):
            with open(fp, encoding="utf-8") as f:
                j = json.load(f)
            d = j.get("date")
            if before and d and d >= before:
                continue
            return {nm: v.get("clean") for nm, v in (j.get("spot") or {}).items()
                    if v.get("clean")}
    except Exception as e:
        logger.debug("[511520] 现券净价快照读取失败: %s", e)
    return {}


def portfolio_dy_bp(bonds, spot):
    """组合基准 Δy(bp)：返回 (基准bp, 逐券加权bp, 来源, 失真券数)。

    🔴 2026-11-14 实测：逐券成交 bp 对低流动性老券严重失真（24国开10 0.25bp、
       24农发20 0.31bp，当日几乎无成交），加权后只有 2.59bp，而按实际净值反推
       需要 3.48bp。政金债篮子以曲线平行移动为主因子，故默认用 10Y 活跃券 bp
       作统一基准；逐券 bp 仍算出来写进明细，便于核对与回溯。
    """
    vals = []
    for b in bonds:
        if b.get("qty", 0) <= 0:
            continue
        s = (spot or {}).get(b.get("name")) or {}
        dy = s.get("dy_bp")
        if dy is not None and abs(dy) <= 50:
            vals.append((b, float(dy)))
    if not vals:
        return 0.0, 0.0, "无可用行情", 0
    wsum = sum(b["qty"] for b, _ in vals)
    wavg = sum(b["qty"] * dy for b, dy in vals) / wsum
    sv = sorted(dy for _, dy in vals)
    n = len(sv)
    median_dy = (sv[n // 2] if n % 2 else (sv[n // 2 - 1] + sv[n // 2]) / 2.0) if sv else None
    act = pick_active_cdb10y(spot)
    base, src = (act or {}).get("dy_bp"), "10Y活跃券 %s" % (act or {}).get("name")
    _bad = base is None or abs(base) > 50
    # 🔴 2026-11-21 盘中稳健性增强：活跃券盘中可能暂时无成交，dy_bp 滞留成 0，
    #   而篮子其余券明显在动 → 单券方向信号已"冻结"。此时改用篮子券 Δy 中位数
    #   作组合基准，避免整个篮子盘中估值被一个停滞的活跃券钉死。
    #   判据保守：活跃券 |dy|<0.05bp 且中位数 |dy|>=0.5bp 才切换，正常行情绝不触发。
    if (not _bad and base is not None and abs(base) < 0.05
            and median_dy is not None and abs(median_dy) >= 0.5):
        base, src = median_dy, "篮子内券Δy中位数（活跃券信号冻结）"
        _bad = True
    if _bad and median_dy is not None:
        base, src = median_dy, "篮子内券 Δy 中位数"
    if base is None:
        base = 0.0
    n_bad = sum(1 for _, dy in vals if abs(dy - base) > 1.5)
    return float(base), float(wavg), src, n_bad


# ══════════════ 第三方估值源：CFETS 逐券估值 + 收益率曲线 ══════════════
def _cfets_file(day):
    return os.path.join(DATA_DIR, "cfets_val_%s.json" % str(day).replace("-", ""))


def _num(row, i):
    try:
        return float(row[i])
    except (ValueError, IndexError, TypeError):
        return None


def _parse_inline_xlsx(content):
    """货币网导出的 xlsx 单元格全是 inlineStr，用标准库解析，不引第三方依赖。"""
    import io
    import zipfile
    import html as _html
    z = zipfile.ZipFile(io.BytesIO(content))
    sh = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "ignore")
    rows = []
    for r in re.findall(r"<row[^>]*>(.*?)</row>", sh, re.S):
        cells = re.findall(
            r"<c\b[^>]*>\s*<is><t[^>]*>(.*?)</t></is>\s*</c>", r, re.S)
        if cells:
            rows.append([_html.unescape(c).strip() for c in cells])
    return rows


def fetch_cfets_valuation(day, force=False):
    """某日 CFETS 逐券估值 {债券代码: {full, net, y, dur, bpv}}。

    🔴 走 ClsBndValExcel 全量导出：3 个请求拿全部券，避开逐券查询的 WAF 限流。
    结果按日落盘（日级缓存）；当日估值尚未发布时写「空结果」短缓存，
    避免看板 30 秒一刷就重复请求。
    """
    day = str(day)[:10]
    if not force and day in _cfets_cache:
        return _cfets_cache[day]
    fp = _cfets_file(day)
    if not force and os.path.exists(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                j = json.load(f)
            # 🔴 2026-11-21 修复：空结果「隔日自动重试」。
            #   旧逻辑只靠 VAL_TTL(30分钟)：某日 17:00 前抓成 empty 后，30 分钟
            #   一过就永久信任空缓存 → 历史静态估值长期退化成期货代理（误差 ~4bp）。
            #   实测 11-17 当日 17:41 抓空（估值尚未发布），隔日补拉即得 252 只券。
            #   新逻辑：仅「当日抓空且缓存<30分钟」才短暂信任；跨日的空缓存不信任，
            #   落下去重抓一次，让临时抓空自愈。有数据的缓存仍永久有效。
            if j.get("empty"):
                _at = j.get("at", 0)
                _same_day = (datetime.fromtimestamp(_at).date().isoformat() == day)
                if (time.time() - _at) < VAL_TTL and _same_day:
                    _cfets_cache[day] = {}
                    return {}
                # 跨日空缓存 → 落到下面正常抓取流程重抓
            elif j.get("data"):
                _cfets_cache[day] = j["data"]
                return j["data"]
        except Exception:
            pass
    out, errs = {}, []
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        for bt, nm in _CFETS_BTYPES:
            u = (_CFETS_BK + "cm-u-bk-bond/ClsBndValExcel?lang=CN&bondType=%s"
                 "&showDate=%s&bondCcy=&bondCode=" % (bt, day))
            got = None
            # 只重试 2 次：当日 17:00 前估值未发布是常态，不能让看板首屏等太久
            for attempt in range(2):
                try:
                    time.sleep(0.3)
                    r = _req("get", u, timeout=15,
                             headers={"User-Agent": _UA, "Referer": _CFETS_REF},
                             impersonate=None)
                    got = r.content
                    if got[:2] == b"PK":
                        break
                    raise RuntimeError("非 xlsx 响应（疑似 WAF 限流）")
                except Exception as e:
                    errs.append("%s#%d:%s" % (nm, attempt + 1, str(e)[:60]))
                    time.sleep(0.8 * (attempt + 1))
            if not got or got[:2] != b"PK":
                # 整批作废：只要有一类取不到，就不要用半份数据估值，
                # 也别继续重试余下两类（当日估值未发布是常态，会白等好几秒）。
                out = {}
                break
            for row in _parse_inline_xlsx(got)[1:]:
                if len(row) < 9:
                    continue
                full, code = _num(row, 4), str(row[2]).strip()
                if full is None or not code:
                    continue
                out[code] = {"name": row[1], "full": full, "net": _num(row, 5),
                             "y": _num(row, 6), "dur": _num(row, 8),
                             "bpv": _num(row, 10)}
    except Exception as e:
        errs.append(str(e)[:100])
    try:
        with open(fp, "w", encoding="utf-8") as f:
            if out:
                json.dump({"date": day, "data": out}, f, ensure_ascii=False)
            else:
                json.dump({"date": day, "empty": True, "at": time.time()}, f)
    except Exception:
        pass
    if out:
        if _val_state["failed"]:
            logger.info("[511520] CFETS 逐券估值已恢复（%s，%d 只）", day, len(out))
            _val_state["failed"] = False
    else:
        # 当日 17:00 前估值未发布是常态，不算故障：debug 留档即可，不刷 warning
        if not _val_state["failed"]:
            logger.debug("[511520] CFETS 逐券估值 %s 暂不可用（稍后自动重试）: %s",
                         day, " | ".join(errs[:2]))
            _val_state["failed"] = True
    _cfets_cache[day] = out
    return out


def cfets_val_change(prev, last, bonds):
    """两日估值全价 → 逐券 ΔP(%)。只返回两日都命中的券。

    估值全价已含应计利息，因此命中该路径的券**不再叠加 carry**。
    """
    out = {}
    for b in bonds:
        a, c = prev.get(b["code"]), last.get(b["code"])
        if a and c and a.get("full") and c.get("full"):
            out[b["code"]] = (c["full"] / a["full"] - 1) * 100
    return out


# ── 历史记录页：用「更新后的估值算法」复算静态估值 ──────────────────────
def _cfets_cached_days():
    """本地 CFETS 逐券估值缓存的日期列表（升序，只取有数据的日子）。"""
    import glob
    out = []
    for fp in sorted(glob.glob(os.path.join(DATA_DIR, "cfets_val_*.json"))):
        stem = os.path.basename(fp)[len("cfets_val_"):-len(".json")]
        if not re.fullmatch(r"\d{8}", stem or ""):
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                j = json.load(f)
            if j.get("data"):
                out.append(("%s-%s-%s" % (stem[:4], stem[4:6], stem[6:]),
                            j["data"]))
        except Exception:
            continue
    return sorted(out)


def static_val_from_cfets(code="511520", min_coverage=0.5):
    """历史静态估值 {估值日: {...}}，算法与**实时 CFETS 逐券路径**完全一致。

    对每个本地有 CFETS 逐券估值的交易日 D，取 D 与前一缓存日的估值全价：
        ratio = Σ(数量×10×full_D) / Σ(数量×10×full_prev)   （两日都命中的券）
        static_val(D) = 0.9952 × nav(D-1) × ratio
                        + 0.0048 × nav(D-1) × (1 + GC001 × 天数/365)
    CFETS 估值全价已含应计利息 ⇒ 债券部分不再叠加 carry（与实时口径一致）。
    返回的是 ratio 等中间量，nav(D-1) 由历史页循环提供（避免重复读库）。
    """
    # 🔴 2026-11-21：补拉「最近缺失缓存的工作日」，让历史页也能享受逐券口径。
    #   背景：实时链路只抓「当天」，历史页此前只会读已缓存的日子；某日抓空/漏抓
    #   就永久退化成期货代理。这里对最近 20 天内「工作日 & 无有效缓存」的日子补拉，
    #   fetch_cfets_valuation 自带日级落盘缓存（含上面的隔日重试），成功后不再请求。
    #   限制条数 + 只补工作日，避免历史页首次打开时发出大量网络请求。
    try:
        _existing = {d for d, _ in _cfets_cached_days()}
        _today = date.today()
        _n = 0
        for _back in range(1, 21):
            _day = _today - timedelta(days=_back)
            if _day.weekday() >= 5 or _day.isoformat() in _existing:
                continue
            if _n >= 8:            # 单页最多补 8 天，防止首次加载过慢
                break
            _got = fetch_cfets_valuation(_day.isoformat())
            _n += 1
            if _got:
                _existing.add(_day.isoformat())
    except Exception as e:
        logger.debug("[511520] 历史 CFETS 补拉失败: %s", e)
    days = _cfets_cached_days()
    if len(days) < 2:
        return {}
    pcf = load_pcf_local(code)
    bonds = [b for b in (pcf or {}).get("bonds", []) if b.get("qty", 0) > 0]
    if not bonds:
        return {}
    out = {}
    for i in range(1, len(days)):
        d_prev, prev = days[i - 1]
        d_last, last = days[i]
        v_base = v_est = 0.0
        hits = 0
        for b in bonds:
            a, c = prev.get(b["code"]), last.get(b["code"])
            if a and c and a.get("full") and c.get("full"):
                face = b["qty"] * 10.0
                v_base += face * float(a["full"])
                v_est += face * float(c["full"])
                hits += 1
        if v_base <= 0 or hits < len(bonds) * min_coverage:
            continue
        out[d_last] = {
            "ratio": v_est / v_base,
            "hits": hits, "total": len(bonds),
            "prev_day": d_prev, "last_day": d_last,
            "algo_id": ALGO_ID, "algo_name": ALGO_NAME,
            "src": "CFETS逐券估值全价 %s→%s（%d/%d 只命中）"
                   % (d_prev, d_last, hits, len(bonds)),
        }
    return out


def _curve_interp(pts, t):
    pts = sorted(pts)
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= t <= x1:
            return y0 + (y1 - y0) * (t - x0) / (x1 - x0)
    return pts[-1][1]


def fetch_curve_dy(end_date=None, term=CURVE_TERM, require_date=None,
                   force=False, lookback=12):
    """CFETS 国开收盘曲线 term 年期「最新交易日 vs 前一交易日」的变动(bp)。

    返回 (dy_bp 或 None, 来源说明, meta)。⚠️ 若最新曲线日早于 require_date
    （盘中当日收盘曲线还没发布），返回 None，调用方自动回退成交 bp，不阻断估值。
    """
    now = time.monotonic()
    end = end_date or date.today()
    if isinstance(end, str):
        end = date.fromisoformat(end[:10])
    # ⚠️ 缓存键必须含 require_date：否则回测 11-13 的曲线 Δy 会被盘中 11-14 误用
    key = (end.isoformat(), float(term), str(require_date))
    if (not force and _curve_cache["v"] and _curve_cache["key"] == key
            and now - _curve_cache["t"] < VAL_TTL):
        return _curve_cache["v"]
    # ⚠️ pageSize 只能是 50：实测 100（以及 500）会被 WAF 直接挡成 403 文本页。
    #    返回按日期倒序，首页 50 条已足够覆盖最近两个交易日 × 15 个关键期限。
    u = (_CFETS_BK + "cm-u-bk-currency/ClsYldCurvHis?lang=CN&reference=1"
         "&bondType=%s&startDate=%s&endDate=%s&termId=%g&pageNum=1&pageSize=50"
         % (CURVE_BOND_TYPE, (end - timedelta(days=lookback)).isoformat(),
            end.isoformat(), term))
    try:
        j = _post(u, {}, _CURVE_REF, timeout=15)
        by = {}
        for r in (j.get("records") or []):
            try:
                by.setdefault(r.get("newDateValueCN"), {})[
                    float(r.get("yearTermStr"))] = float(r.get("maturityYieldStr"))
            except (TypeError, ValueError, AttributeError):
                continue
        days = sorted(d for d in by if d)
        if len(days) < 2:
            raise RuntimeError("曲线样本不足")
        d_prev, d_last = days[-2], days[-1]
        if require_date and d_last < str(require_date)[:10]:
            raise RuntimeError("当日曲线尚未发布（最新 %s）" % d_last)
        y_prev = _curve_interp(list(by[d_prev].items()), term)
        y_last = _curve_interp(list(by[d_last].items()), term)
        dy = (y_last - y_prev) * 100.0
        if abs(dy) > 50:
            raise RuntimeError("曲线变动异常 %.2fbp" % dy)
        res = (dy, "CFETS国开收盘曲线 %gY (%s→%s)" % (term, d_prev, d_last),
               {"最新曲线日": d_last, "前一曲线日": d_prev,
                "收益率%": round(y_last, 4), "前一日收益率%": round(y_prev, 4),
                "样本天数": len(days), "关键期限": term})
    except Exception as e:
        res = (None, "曲线不可得，回退成交bp（%s）" % str(e)[:60],
               {"错误": str(e)[:140]})
    _curve_cache.update(t=now, key=key, v=res)
    return res


def _calib_file():
    return os.path.join(DATA_DIR, "calib.json")


def load_calib():
    try:
        with open(_calib_file(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def record_calib(bias_bp, asof):
    """盘后净值公布后记录一次「模型 vs 实际」口径残差（同一天只记一次）。

    🔴 只在官方净值已消化当日行情时调用，避免拿盘中未完成的数据自我校准。
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        old = load_calib()
        if old.get("asof") == asof:
            return old
        if abs(bias_bp) > CALIB_MAX_BP:
            logger.debug("[511520] 口径残差 %.2fbp 超阈值，不纳入校准", bias_bp)
            return old
        prev = old.get("ema_bp", bias_bp)
        rec = {"asof": asof, "last_bp": round(bias_bp, 3),
               "ema_bp": round(prev + CALIB_EMA_ALPHA * (bias_bp - prev), 3),
               "samples": int(old.get("samples", 0)) + 1}
        with open(_calib_file(), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        return rec
    except Exception as e:
        logger.debug("[511520] 校准记录写入失败: %s", e)
        return {}


def resolve_calib(target=None):
    """返回 (校准bp, 来源)。手动 CALIB_BP 优先；否则按开关取自动 EMA。

    🔴 防前视：校准样本来自「净值已公布」的盘后，只能用于**更晚**的估值日。
       回测历史日期时若样本日 >= 估值目标日，必须跳过，否则等于拿答案校准答案。
    """
    if CALIB_BP:
        return CALIB_BP, "手动常量 CALIB_BP"
    if not CALIB_AUTO:
        return 0.0, "未启用"
    c = load_calib()
    if target and c.get("asof") and str(c["asof"]) >= str(target):
        return 0.0, "样本日 %s 不早于估值日 %s，已跳过（防前视）" % (c["asof"], target)
    bp = float(c.get("ema_bp") or 0.0)
    if abs(bp) > CALIB_MAX_BP:
        return 0.0, "自动值超阈值，已忽略"
    return bp, "自动EMA（截至 %s，样本 %s）" % (c.get("asof"), c.get("samples", 0))


# ════════════════════════════ 国开10Y活跃利率债（仅作方向参考）════════════════════════════
def _real_duration(code, day=None):
    """CFETS 官方修正久期（优先），取不到返回 None。

    🔴 2026-11-16：`estimate_duration` 是按「发行年 → 剩余年限 × 0.9」的粗估，
       对政金债普遍高估 5%~15%（26国开10 估 9.00/10.00，真实 8.76；
       24农发10 估 7.20，真实 6.65）。凡是「Δy × 久期」折价格的路径都应优先用
       真实久期，否则跌幅会被系统性放大。
       day 默认取最新交易日（估值发布日比行情晚一天，故先试今天再回退昨天）。
    """
    days = []
    if day:
        days.append(str(day)[:10])
    else:
        today = date.today()
        for k in (0, 1, 2, 3):
            days.append((today - timedelta(days=k)).strftime("%Y-%m-%d"))
    for d in days:
        try:
            v = fetch_cfets_valuation(d).get(str(code)) or {}
        except Exception:
            continue
        if v.get("dur"):
            return float(v["dur"])
    return None


def _real_duration_map(codes, day=None):
    """批量取真实久期 {代码: dur}，缺失的不列入（调用方自行兜底估算）。"""
    out = {}
    for c in codes or []:
        d = _real_duration(c, day)
        if d:
            out[str(c)] = d
    return out


def pick_active_cdb10y(spot=None):
    """从货币网成交行情里挑「国开10Y活跃券」：剩余期限 8.3~10.6Y、名称含国开、
    取代码最新（新券即活跃券）。返回 dict 或 None。

    ⚠️ 两处用途（东哥 2026-11-16 明确口径）：
       1) 兜底估值：PCF 清单里某只券当天**没有成交额**时，用它的 Δy × 该券久期推算价格；
       2) 展示：看板「指数价 / 指数涨幅」列。
       有成交的券一律走真实净价，不受它影响。
    """
    spot = spot if spot is not None else fetch_spot_deal()
    cands = []
    for nm, v in (spot or {}).items():
        if "国开" not in nm or not v.get("clean") or v.get("term") is None:
            continue
        if not (ACTIVE_MIN_Y <= v["term"] <= ACTIVE_MAX_Y):
            continue
        code = v.get("code") or ""
        if not re.fullmatch(r"\d{6}", code):      # 排除 09xxxxxx 等特殊代码
            continue
        cands.append((code, nm, v))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0], reverse=True)   # 代码最新 = 最新发行 = 活跃
    code, nm, v = cands[0]
    # 🔴 2026-11-16：优先用 CFETS 真实修正久期，取不到才退回按发行年估算。
    #   估算久期对 26国开10 给 10.00（含 1.0 放大），真实修正久期是 8.76，
    #   直接拿它算涨幅会把跌幅放大 14%（-0.45% vs 真实 -0.40%）。
    dur = _real_duration(code) or estimate_duration(code, scale=1.0)
    dy_bp = v.get("dy_bp") or 0.0
    # 净价涨跌幅% ≈ -修正久期 × Δy(%)；Δy(bp)→% 需 /100
    px_pct = -(dur / 100.0) * dy_bp
    return {"code": code, "name": nm, "clean": v.get("clean"),
            "yield": v.get("yield"), "dy_bp": dy_bp, "dur": round(dur, 4),
            "dur_src": "CFETS估值" if _real_duration(code) else "按发行年估算",
            "price_pct": round(px_pct, 4), "term": v.get("term"),
            "show_time": v.get("show_time")}


def _src_mix(rows):
    """按「来源」汇总各估值路径的券数与权重占比（核对用）。"""
    agg = {}
    for r in rows or []:
        k = str(r.get("来源") or "?")
        a = agg.setdefault(k, [0, 0.0])
        a[0] += 1
        a[1] += (r.get("权重") or 0) * 100
    return {"%s" % k: {"只数": v[0], "权重%": round(v[1], 2)}
            for k, v in sorted(agg.items(), key=lambda x: -x[1][1])}


# ════════════════════════════ 估值核心 ════════════════════════════
def _accrual_days(nav_date, target_date):
    """官方净值日 → 估值日 的自然日数（跨周末自动 3 日）。"""
    return max(1, min(_trading_days_between(nav_date, target_date), 10))


def estimate_511520_nav(nav_info=None, pcf=None, spot=None, prev_pcf=None,
                        spot_prev=None, daily_accrual=None, target_date=None):
    """511520 逐券 PCF 实时估值（方法二）。

    参数
      nav_info   {nav, nav_date}  最新官方净值；None 则内部现拉
      pcf        富国官网 PCF；None 则内部抓
      spot       货币网成交行情；None 则内部抓
      prev_pcf   上一交易日 PCF（可选，用于「两日实际价格变化」附加参考）
      spot_prev  上一交易日净价快照 {简称: 净价}（可选，有则用真实净价变动）
      daily_accrual None（默认）= 逐券按票面利率计算 carry；
                    显式给数值则作为固定 carry 覆盖（调试/回溯用）
      target_date   估值目标日 YYYY-MM-DD（默认今天；回测历史日期时显式传）

    返回 dict
      est        估算净值（None=取数失败）
      idx_chg    国开10Y活跃券净价涨跌%（仅参考）
      index_nm   "国开10Y活跃利率债"
      idx_price  活跃券成交净价（仅参考）
      method     口径标识
      note       异常说明
      detail     逐券明细 / 篮子 / 应计 / 活跃券，便于核对与记录
    """
    out = {"est": None, "idx_chg": None, "index_nm": None, "idx_price": None,
           "method": "pcf_basket", "note": None,
           "latest_nav": None, "nav_date": None, "detail": {},
           "algo_id": ALGO_ID, "algo_name": ALGO_NAME}

    # ---- 官方净值
    if nav_info is None:
        try:
            import bond_etf_valuation as _bv
            hist = _bv._nav_cache.get_history("511520", days=30)
            if hist:
                nav_info = {"nav": hist[0][1], "nav_date": hist[0][0]}
        except Exception:
            nav_info = None
    if not nav_info or not nav_info.get("nav"):
        out["note"] = "官方净值获取失败"
        return out
    nav0, nav_date = float(nav_info["nav"]), nav_info["nav_date"]
    out["latest_nav"], out["nav_date"] = nav0, nav_date

    # ---- PCF 清单
    if pcf is None:
        try:
            pcf = fetch_pcf_online("511520")
        except Exception as e:
            out["note"] = "PCF 清单获取失败: %s" % e
            out["method"] = "pcf_unavailable"
            return out

    # ---- 货币网现券成交
    if spot is None:
        spot = fetch_spot_deal()

    target = target_date or date.today().strftime("%Y-%m-%d")
    days = _accrual_days(nav_date, target)

    bonds = [b for b in pcf["bonds"] if b["qty"] > 0]      # 数量为0剔除
    if not bonds:
        out["note"] = "PCF 无非零数量成份券"
        return out
    total_qty = sum(b["qty"] for b in bonds)
    # 组合久期：优先 CFETS 真实修正久期，缺失的券退回按发行年估算。
    # 🔴 2026-11-16：旧代码全用估算久期，组合久期 7.99，真实 7.55（高估 5.8%）。
    _dur_real = _real_duration_map([b["code"] for b in bonds], day=nav_date)
    dur_w = sum((b["qty"] / total_qty)
                * (_dur_real.get(b["code"]) or estimate_duration(b["code"]))
                for b in bonds)

    # ---- 日期对齐（2026-11-14 估值差归因后新增，最关键的一步）
    #   PCF(D) 的「替代金额」对应 **D-1 收盘**（篮子恒等式 = 前一交易日净值，
    #   本例 119.49757 = 11-12 官方净值，偏差 -0.003bp）。货币网 show_time 是行情日。
    #   若「行情日 <= 最新官方净值日」，说明这批行情已被净值消化；再叠加一次就是
    #   重复计算，会把「11-13 的回溯估值」冒充成「当前估值」输出 —— 这正是
    #   est=119.2517 对不上实际净值 119.1596 的首要原因。
    #   🔴 但「价格效应归零」≠「直接返回官方净值」：盘后/周末仍按逐券票息+逆回购
    #      计息算出 est，官方净值只作锚点 nav0。2026-11-14 东哥要求「按计算来」。
    sp_date = _latest_spot_date(spot)
    nav_base_abs = ((sum(b["amount"] for b in bonds) + pcf.get("cash", 0.0))
                    / pcf["min_unit"]) if pcf.get("min_unit") else nav0
    pcf_lag = abs(nav_base_abs - nav0) > 0.01
    no_new_quote = bool(sp_date and nav_date and sp_date <= nav_date)

    # 口径残差：只做诊断展示，绝不参与估值（不做拟合）
    diag = None
    if no_new_quote:
        base_dy0, w_raw_dy0, dy_src0, n_bad0 = portfolio_dy_bp(bonds, spot)
        carry_avg0, _, _ = portfolio_daily_carry(bonds, spot)
        model_ret = -dur_w * (base_dy0 / 10000.0) + carry_avg0 * days / 100.0
        actual_ret = nav0 / nav_base_abs - 1 if nav_base_abs else 0.0
        diag = {"实际净值变动bp": round(actual_ret * 10000, 3),
                "模型隐含变动bp": round(model_ret * 10000, 3),
                "残差bp": round((actual_ret - model_ret) * 10000, 3),
                "基准Δy_bp": round(base_dy0, 3),
                "逐券加权Δy_bp": round(w_raw_dy0, 3),
                "Δy来源": dy_src0, "失真券数": n_bad0,
                "提示": "仅诊断；残差属成交口径 vs 第三方估值口径的结构差，不用于修正估值"}
        if CALIB_AUTO:
            diag["已记入校准"] = record_calib(diag["残差bp"], nav_date)

    # 旧口径（默认关闭）：极少数场景想直接看官方净值时可显式打开
    if no_new_quote and FREEZE_OFFICIAL_NAV:
        out["est"] = round(nav0, 4)
        out["method"] = "official_nav_frozen"
        out["note"] = ("显式开关 FREEZE_OFFICIAL_NAV=True：显示官方净值 %.4f（非计算值）"
                       % nav0)
        out["detail"] = {"估值状态": "官方净值冻结（旧口径，非模型计算）",
                         "模型口径偏差": diag, "组合久期": round(dur_w, 4)}
        return out

    # ---- 逐券 carry：默认按成份券反推票面利率，不再用固定 6.5 厘
    if daily_accrual is not None:
        carry_avg, carry_map, n_carry = float(daily_accrual), {}, 0
        carry_src = "外部指定固定值"
    else:
        carry_avg, carry_map, n_carry = portfolio_daily_carry(bonds, spot)
        carry_src = ("逐券反推票面利率/365（命中 %d/%d 只）"
                     % (n_carry, len(bonds)))
        if n_carry == 0:
            carry_src = "行情缺失，回退兜底固定值"
    acc_avg = carry_avg * days

    # 上一交易日净价快照：有就走「真实两日净价变动」，零推算（优先级最高）
    # 🔴 无新增行情时禁用：净价差/Δy 都是上一交易日的，已被官方净值消化，再叠加即重复计算
    if not no_new_quote and not spot_prev:
        spot_prev = load_spot_prev(before=sp_date)
    spot_prev = {} if no_new_quote else (spot_prev or {})

    # 组合 Δy 口径：默认曲线平行移动（10Y活跃券基准），逐券成交 bp 失真大
    base_dy_raw, w_raw_dy, dy_src, n_bad = portfolio_dy_bp(bonds, spot)
    combo_dy = (None if no_new_quote else
                (base_dy_raw if (DY_MODE == "curve" and base_dy_raw) else None))

    # ---- 第三方估值源（VAL_SOURCE，2026-11-14）
    #   cfets : 两日逐券估值全价变动 → 零推算、最贴近基金落账口径；
    #           当日 17:00 前估值未发布时自动回退「曲线Δy × 真实久期」
    #   curve : 只把组合 Δy 换成 CFETS 收盘曲线 10Y 关键期限的日变动
    #   spot  : 完全旧口径（成交 bp × 按发行年估算的久期）
    #   三者共享 CFETS 真实修正久期 mdfdDrtn，比 estimate_duration 的估算准。
    _val_mode = ("carry_only" if no_new_quote else
                 (VAL_SOURCE if DY_MODE != "per_bond" else "spot"))
    curve_dy, curve_src, curve_meta = None, "未启用", {}
    cfets_dp, cfets_ref = {}, {}
    val_prev_day = val_last_day = None
    n_cfets = n_dur_real = 0
    if _val_mode in ("cfets", "curve"):
        curve_dy, curve_src, curve_meta = fetch_curve_dy(
            end_date=target, require_date=sp_date)
        if curve_dy is not None:
            combo_dy = curve_dy
            dy_src = curve_src
    if _val_mode in ("cfets", "curve"):
        # 前一交易日的估值一定有 → 用它提供**真实修正久期/基点价值**，
        # 替换 estimate_duration() 按发行年的粗略估算（两个模式都受益）。
        val_prev_day = str(nav_date)[:10]
        val_last_day = str(sp_date or target)[:10]
        _prev = fetch_cfets_valuation(val_prev_day) if val_prev_day else {}
        _last = {}
        if _val_mode == "cfets" and val_last_day > val_prev_day:
            # 只有 cfets 模式才尝试当日估值；curve 模式不去碰尚未发布的数据
            _last = fetch_cfets_valuation(val_last_day)
        cfets_ref = _last or _prev or {}
        if _val_mode == "cfets" and _prev and _last:
            cfets_dp = cfets_val_change(_prev, _last, bonds)

    v_base = v_est = 0.0
    n_spotpx = n_spot = n_pcf = 0
    rows = []
    for b in bonds:
        px_base = b["amount"] / (b["qty"] * 10)            # 全价/百元（含应计）
        ref = cfets_ref.get(b["code"]) or {}
        dur = ref.get("dur") or estimate_duration(b["code"])
        if ref.get("dur"):
            n_dur_real += 1
        sp = (spot or {}).get(b["name"])
        prev_px = spot_prev.get(b["name"])
        src = "PCF推算"
        dy_i = None
        px_clean = None
        acc_override = None
        if no_new_quote:
            # 0) 盘后/周末：行情已被官方净值消化 ⇒ 价格效应归零，只按逐券票息计息。
            #    est 仍是模型算出来的（nav0 + 票息 + 逆回购），不是把官方净值原样显示。
            dp = 0.0
            src = "无新增行情(仅逐券carry)"
            n_pcf += 1
        elif _val_mode == "cfets" and b["code"] in cfets_dp:
            # a0) 两日 CFETS 估值全价变动（第三方估值口径，零推算）。
            #     🔴 估值全价已含应计利息，本路径**不再叠加 carry**，否则重复计息。
            dp = cfets_dp[b["code"]]
            px_clean = ref.get("net")
            src = "CFETS估值全价两日变动"
            acc_override = 0.0
            n_cfets += 1
        elif sp and prev_px and prev_px > 0 and sp.get("clean"):
            # a) 两日真实净价 → 直接净价变动
            px_clean = sp["clean"]
            dp = (px_clean / prev_px - 1) * 100
            src = "净价实际变动"
            n_spotpx += 1
        elif sp and (combo_dy is not None or
                     (sp.get("dy_bp") is not None and abs(sp["dy_bp"]) <= 50)):
            # b) 收益率变动(bp) × 该券久期
            #    🔴 口径（东哥 2026-11-16 明确）：511520 以 **PCF 清单逐券成交价**为主；
            #       该券当天有成交净价就走 a)（真实净价差）；只有净价拿不到时才用
            #       **国开10Y活跃利率债** 的 Δy 作平行移动基准。
            #   DY_MODE="curve"   → 用组合基准 Δy（= 国开10Y活跃券，平行移动假设）
            #   DY_MODE="per_bond"→ 逐券成交 bp（回溯/对比用）
            px_clean = sp.get("clean")
            if combo_dy is not None:
                dy_i, src = combo_dy, "净价+国开10Y活跃券Δy"
            else:
                dy_i, src = sp["dy_bp"], "净价+本券Δy"
            dp = -dur * (dy_i / 10000.0) * 100
            n_spot += 1
        else:
            # c) 兜底：本券当日**无成交额**（低流动性老券整天没成交、货币网不返回）。
            #    🔴 东哥 2026-11-16 明确：这时就用 **国开10Y活跃利率债** 的 Δy 估值。
            #      活跃10Y券是全市场流动性最好的利率债，其 Δy 代表当日利率曲线主因子；
            #      政金债篮子与该曲线的相关性极高，拿它作平行移动基准是标准做法。
            # 🔴 2026-11-21：优先用已做稳健处理的 combo_dy（活跃券正常时就是它的
            #   Δy；活跃券冻结时 portfolio_dy_bp 已切到篮子中位），避免直接用
            #   可能停滞的活跃券 dy_bp=0 导致无成交券价格"冻结"不动。
            act = pick_active_cdb10y(spot)
            dy_p = (combo_dy if combo_dy is not None else
                    ((act or {}).get("dy_bp") or 0.0))
            dp = -dur * (dy_p / 10000.0) * 100
            src = "无成交→组合基准Δy(活跃券/篮子中位)"
            dy_i = dy_p
            n_pcf += 1
        carry_i = carry_map.get(b["name"], carry_avg)
        acc_i = carry_i * days if acc_override is None else acc_override
        px_est = px_base * (1 + dp / 100) + acc_i       # 补本券应计利息 → 全价
        v_base += b["qty"] * 10 * px_base
        v_est += b["qty"] * 10 * px_est
        coupon_i = (carry_i * ACC_YEAR_DAYS) if carry_i is not None else None
        rows.append({"代码": b["code"], "简称": b["name"], "数量": b["qty"],
                     "权重": round(b["qty"] / total_qty, 6), "久期": dur,
                     "久期来源": "CFETS估值" if ref.get("dur") else "按发行年估算",
                     "估值基点价值": ref.get("bpv"),
                     "PCF全价": round(px_base, 6), "成交净价": px_clean,
                     "本券Δy_bp": dy_i, "原始成交bp": (sp or {}).get("dy_bp"),
                     "反推票息%": round(coupon_i, 4)
                     if coupon_i is not None else None,
                     "日carry元/百元": round(carry_i, 8)
                     if carry_i is not None else None,
                     "应计合计": round(acc_i, 6),
                     "应计说明": "估值全价已含，不叠加" if acc_override is not None
                                 else "按票息/365 逐日计提",
                     "估值全价": round(px_est, 6),
                     "ΔP%": round((px_est / px_base - 1) * 100, 6), "来源": src})

    # ---- 汇总净值：债券 99.52% + 一日逆回购 0.48%
    #   🔴 只取篮子的「相对变动」并锚定最新官方净值，而不是直接拿篮子绝对价当净值：
    #      PCF 替代金额是申赎口径，与官方净值之间可能存在估值机构/费用/舍入基差，
    #      锚定后基差自动消掉；篮子恒等式成立时两式等价（实测偏差 -0.003bp）。
    nav_basket_abs = ((v_est + pcf.get("cash", 0.0)) / pcf["min_unit"]
                      if pcf.get("min_unit") else nav0)
    nav_basket = (nav0 * (nav_basket_abs / nav_base_abs)
                  if nav_base_abs > 0 else nav_basket_abs)
    r_repo, repo_src = fetch_repo_rate()
    nav_repo = nav0 * (1.0 + r_repo * days / ACC_YEAR_DAYS)
    # 🔴 纯计算：默认不乘任何校准/拟合系数（CALIB_APPLY=False）
    calib_bp, calib_src = (resolve_calib(target) if CALIB_APPLY else
                           (0.0, "未启用（严格按模型计算，不做拟合）"))
    nav_est = ((BOND_WEIGHT * nav_basket + REPO_WEIGHT * nav_repo)
               * (1.0 + calib_bp / 10000.0))
    repo_income = REPO_WEIGHT * (nav_repo - nav0)
    out["est"] = round(nav_est, 4)
    out["method"] = "pcf_basket_spot"
    if no_new_quote:
        out["method"] = "pcf_basket_carry_only"
        out["note"] = ("最新行情 %s 已被官方净值 %s 消化，价格效应归零；"
                       "按逐券票息 + 逆回购计息计算 %.4f（未冻结官方净值、未拟合）"
                       % (sp_date, nav_date, nav_est))
    elif n_cfets == len(bonds):
        out["method"] = "pcf_basket_cfets_val"
    elif n_spotpx == len(bonds):
        out["method"] = "pcf_basket_spot_realpx"
    elif n_spot == len(bonds):
        out["method"] = "pcf_basket_spot_dy"
    elif n_spotpx + n_pcf == len(bonds):
        # PCF 逐券：有成交的走真实净价，无成交的用国开10Y活跃券 Δy（东哥 11-16 口径）
        out["method"] = "pcf_basket_pcf_plus_active10y"
        out["note"] = ("%d/%d 只按 PCF 逐券真实净价，%d 只无成交额、"
                       "用国开10Y活跃利率债Δy×久期推算"
                       % (n_spotpx, len(bonds), n_pcf))
    elif n_pcf == len(bonds):
        out["method"] = "pcf_basket_est"
        out["note"] = "现券行情不可得，逐券用国开10Y活跃券Δy推算"
    if pcf.get("stale"):
        # 说清真相，别让用户误以为"官网改版了"。两种降级原因要说成两句话：
        #   · 网络不通   → 提示几秒后自动重试（去查代理）
        #   · 当日未发布 → 提示非交易日/尚未披露（无需任何操作）
        _src = pcf.get("source") or ""
        if "尚未发布" in _src:
            _m = ("当日 PCF 尚未发布（非交易日或官网未披露），用本地快照 %s 估值；"
                  "披露后自动切回在线" % pcf.get("date"))
        else:
            _left = _backoff_left()
            _when = ("%.0f秒后自动重试" % _left) if _left > 0 else "下次请求即重试"
            _m = "网络暂不可达，PCF 用本地快照 %s 估值（%s）" % (pcf.get("date"), _when)
        out["note"] = (_m + "；" + out["note"]) if out.get("note") else _m

    # ---- 国开10Y活跃利率债：展示「指数价/指数涨幅」+ 无成交券的兜底估值基准
    #   🔴 东哥 2026-11-16 口径：有成交的券（97%+ 权重）一律走真实净价，
    #      只有当日无成交额的那几只才拿它的 Δy × 该券久期推算。
    act = pick_active_cdb10y(spot)
    if act:
        out["index_nm"] = "国开10Y活跃利率债"
        out["idx_price"] = act["clean"]
        out["idx_chg"] = act["price_pct"]

    # ---- 附加：两日 PCF 实际价格变化（有历史清单时）
    nav2_real = None
    prev_codes = {b["code"]: b for b in (prev_pcf or {}).get("bonds", []) if b["qty"] > 0}
    if prev_codes:
        matched = 0
        v_now = v_prev = 0.0
        for b in bonds:
            p = prev_codes.get(b["code"])
            px_now = b["amount"] / (b["qty"] * 10)
            if p and p["qty"] > 0 and p["amount"] > 0:
                px_prev = p["amount"] / (p["qty"] * 10)
                matched += 1
            else:
                px_prev = px_now
            v_now += b["qty"] * 10 * px_now
            v_prev += b["qty"] * 10 * px_prev
        if matched and v_prev > 0:
            base = (prev_pcf or {}).get("nav") or nav0
            nav2_real = round(base * (v_now / v_prev), 4)

    out["detail"] = {
        "算法": {"id": ALGO_ID, "名称": ALGO_NAME, "版本": ALGO_VERSION,
                 "入口": "core/bond_pcf_valuation.estimate_511520_nav",
                 "口径": VAL_SOURCE, "拟合": "关闭（严格按模型计算）"},
        "价格口径": "全价(含应计利息)",
        "估值状态": ("盘后/非交易日：无新增行情，价格效应归零，"
                   "按逐券票息+逆回购计息纯计算（不冻结官方净值、不拟合）"
                   if no_new_quote else "盘中：按当日行情逐券重估全价"),
        "模型口径偏差": diag,
        "日期对齐": {"官方净值日": nav_date, "官方净值": nav0,
                     "行情日期": sp_date, "PCF日期": pcf.get("date"),
                     "PCF篮子隐含净值": round(nav_base_abs, 6),
                     "PCF基准落后净值日": pcf_lag, "估值目标日": target,
                     "说明": "PCF(D) 对应 D-1 收盘；只叠加比官方净值更新的行情；"
                             "官方净值仅作锚点，不作为 est 输出"},
        "Δy口径": {"模式": DY_MODE, "基准Δy_bp": round(base_dy_raw, 3),
                   "本轮采用Δy_bp": round(combo_dy, 3) if combo_dy is not None else None,
                   "逐券加权Δy_bp": round(w_raw_dy, 3), "Δy来源": dy_src,
                   "曲线Δy_bp": round(curve_dy, 3) if curve_dy is not None else None,
                   "曲线来源": curve_src, "曲线明细": curve_meta,
                   "失真券数": n_bad,
                   "说明": ("511520 以 PCF 清单逐券估值为主：有成交的券用真实净价差；"
                            "无成交额的券用国开10Y活跃券Δy×久期推算（平行移动假设）")},
        "估值源": {"模式": _val_mode,
                   "CFETS基准日": val_prev_day, "CFETS最新日": val_last_day,
                   "估值全价两日变动命中": "%d/%d" % (n_cfets, len(bonds)),
                   "真实修正久期命中": "%d/%d" % (n_dur_real, len(bonds)),
                   "说明": ("无新增行情时不取第三方估值，只按逐券票息计息"
                            if no_new_quote else
                            "CFETS 逐券估值(公开)≠中债估值(授权)；当日 17:00 前未发布时自动回退曲线Δy×真实久期")},
        "篮子锚定": {"PCF篮子绝对净值": round(nav_basket_abs, 6),
                     "锚定后净值": round(nav_basket, 6),
                     "锚点": "最新官方净值 %.4f" % nav0},
        "两日净价快照": {"命中": n_spotpx, "共": len(bonds),
                         "无成交用活跃券Δy": n_pcf,
                         "说明": "有成交：真实净价变动（零推算）；"
                                 "无成交额：国开10Y活跃券Δy×该券久期"},
        "估值来源构成": _src_mix(rows),
        "PCF日期": pcf.get("date"), "PCF来源": pcf.get("source"),
        "校准": {"采用bp": calib_bp, "来源": calib_src,
                 "拟合总开关": CALIB_APPLY, "自动开关": CALIB_AUTO,
                 "手动常量": CALIB_BP,
                 "说明": "默认 0bp：估值完全由模型计算，不用历史残差贴答案"},
        "价格来源": {"CFETS估值全价": n_cfets, "净价实际变动": n_spotpx,
                     "Δy推算": n_spot, "仅carry": n_pcf if no_new_quote else 0,
                     "PCF推算": 0 if no_new_quote else n_pcf, "共": len(bonds),
                     "Δy模式": ("无新增行情(价格效应归零)" if no_new_quote else
                                ("组合基准(曲线/活跃券)" if combo_dy is not None
                                 else "逐券成交bp(旧口径)"))},
        "最小申赎单位": pcf.get("min_unit"), "预估现金": pcf.get("cash"),
        "篮子校验": {"隐含净值": pcf.get("basket_implied"),
                     "官方净值": pcf.get("nav"),
                     "偏差bp": round(pcf["basket_diff_bp"], 3)
                     if pcf.get("basket_diff_bp") is not None else None},
        "应计利息": {"算法": carry_src,
                     "组合加权日carry元/百元": round(carry_avg, 8),
                     "旧固定值": DAILY_ACCRUAL,
                     "天数": days, "合计元/百元": round(acc_avg, 6),
                     "逐券命中": "%d/%d" % (n_carry, len(bonds))},
        "仓位": {"债券": BOND_WEIGHT, "一日逆回购": REPO_WEIGHT,
                 "逆回购年化": round(r_repo, 6), "逆回购利率来源": repo_src,
                 "计息天数": days,
                 "债券篮子净值": round(nav_basket, 4),
                 "逆回购净值": round(nav_repo, 4),
                 "逆回购贡献元/份": round(repo_income, 6)},
        "组合久期": round(dur_w, 4),
        "篮子估值前": round(v_base, 4), "篮子估值后": round(v_est, 4),
        "活跃券": act, "两日PCF实际": nav2_real,
        "成份券": rows,
    }
    return out


def clear_caches(reset_net=False):
    """清数据缓存。reset_net=True 时连「链路锁定/退避窗口」一起清掉
    （手动刷新按钮应在网络恢复后能立刻重试，不被退避挡住）。"""
    with _pcf_lock:
        _pcf_cache["t"] = 0.0
        _pcf_cache["v"] = None
    with _spot_lock:
        _spot_cache["t"] = 0.0
        _spot_cache["v"] = None
    _repo_cache.update(t=0.0, rate=None, src=None)
    _cfets_cache.clear()
    _curve_cache.update(t=0.0, key=None, v=None)
    _val_state["failed"] = False
    if reset_net:
        _NET_OK["mode"] = None
        _IMP_OK["fp"] = None          # 2026-11-16：指纹也一起重试（换了网络/代理后指纹可能变）
        _NET_FAIL["until"] = 0.0
        _NET_FAIL["count"] = 0
        _GUESS.update(url=None, at=0.0)   # 强制重扫代理端口
        _pcf_state["stale"] = False
        _spot_state["failed"] = False


# ════════════════════════════ 自检 ════════════════════════════
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO)
    print("=" * 78)
    print("  511520 PCF 逐券实时估值自检")
    print("=" * 78)
    pcf = fetch_pcf_online("511520")
    print("PCF 日期 %s  来源 %s  成份券 %d 只  最小单位 %s  预估现金 %s"
          % (pcf["date"], pcf["source"], len(pcf["bonds"]),
             pcf["min_unit"], pcf["cash"]))
    if pcf.get("basket_implied"):
        print("篮子校验: 隐含 %.4f vs 官方 %.4f，偏差 %+.3fbp"
              % (pcf["basket_implied"], pcf["nav"], pcf["basket_diff_bp"]))
    r = estimate_511520_nav(pcf=pcf)
    d = r["detail"]
    _a = d["应计利息"]
    print("\n最新净值 %.4f [%s]  组合久期 %.2f" %
          (r["latest_nav"], r["nav_date"], d["组合久期"]))
    print("应计利息 %s：加权日carry %.6f 元/百元（旧固定 %.6f）× %s日 = %.6f"
          % (_a["算法"], _a["组合加权日carry元/百元"], _a["旧固定值"],
             _a["天数"], _a["合计元/百元"]))
    _w = d["仓位"]
    print("仓位 债券 %.2f%% + 一日逆回购 %.2f%%（年化 %.4f%%，来源 %s，贡献 %+.6f 元/份）"
          % (_w["债券"] * 100, _w["一日逆回购"] * 100,
             _w["逆回购年化"] * 100, _w["逆回购利率来源"], _w["逆回购贡献元/份"]))
    print("价格来源 %s" % d["价格来源"])
    print("\n%-8s%-11s%6s%8s%7s%10s%10s%9s%10s%9s  %s"
          % ("代码", "简称", "数量", "权重%", "久期", "PCF全价", "成交净价",
             "反推票息%", "估值全价", "ΔP%", "来源"))
    for b in sorted(d["成份券"], key=lambda x: -x["数量"]):
        print("%-8s%-11s%6.0f%8.2f%7.2f%10.4f%10s%9s%10.4f%9.3f  %s"
              % (b["代码"], b["简称"], b["数量"], b["权重"] * 100, b["久期"],
                 b["PCF全价"], ("%.2f" % b["成交净价"]) if b["成交净价"] else "--",
                 ("%.3f" % b["反推票息%"]) if b["反推票息%"] is not None else "--",
                 b["估值全价"], b["ΔP%"], b["来源"]))
    print("\n估值结果 NAV_est = %.4f  (%+.3f%% vs 最新净值)"
          % (r["est"], (r["est"] / r["latest_nav"] - 1) * 100))
    if r["index_nm"]:
        _act = d["活跃券"]
        print("方向参考 %s: %s 净价 %.2f  收益率 %s%%  Δy %+.2fbp  净价涨幅 %+.3f%%"
              % (r["index_nm"], _act["code"], r["idx_price"],
                 ("%.4f" % _act["yield"]) if _act.get("yield") is not None else "--",
                 _act["dy_bp"], r["idx_chg"]))
    if d.get("两日PCF实际"):
        print("两日 PCF 实际价格变化估值: %.4f" % d["两日PCF实际"])
