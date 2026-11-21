# -*- coding: utf-8 -*-
"""新增 9 只现金管理 ETF 到 fund_profile（实时估值待接入）。

用法：
    python core/_add_cash9.py            # 打印将要写入的内容（dry-run）
    python core/_add_cash9.py --apply    # 真正写入

口径说明（2026-11-17）：
  · jsl_market='C' → 出现在看板「现金管理」TAB
  · val_method='pending' → 明确表示「实时估值尚未接入」，
      与 'bond'（已有估值算法）区分开，避免 estimate_bond_nav 返回
      「未知债券基金」后被上层静默回退成「最新净值」冒充实时估值。
  · fund_type='LOF' 与现有 C 市场 7 只保持一致。
"""
from __future__ import print_function

import os
import sqlite3
import sys

CODES = [
    # (代码, 名称, 跟踪指数, 类型备注)
    ("511070", "公司债ETF南方",   "沪做市公司债", "沪市·基准做市公司债"),
    ("511110", "公司债ETF易方达", "沪做市公司债", "沪市·基准做市公司债"),
    ("511190", "信用债ETF海富通", "沪做市公司债", "沪市·基准做市公司债"),
    ("511200", "信用债ETF华夏",   "沪做市公司债", "沪市·基准做市公司债"),
    ("551900", "科创债ETF招商",   "AAA科创债",   "沪市·中证AAA科创债"),
    ("551030", "科创债ETF鹏华",   "沪AAA科创债", "沪市·上证AAA科创债"),
    ("551500", "科创债ETF易方达", "AAA科创债",   "沪市·中证AAA科创债"),
    ("551550", "科创债ETF华夏",   "AAA科创债",   "沪市·中证AAA科创债"),
    ("159600", "科创债ETF嘉实",   "AAA科创债",   "深市·中证AAA科创债"),
]

HERE = os.path.dirname(os.path.abspath(__file__))


def db_path():
    p = os.environ.get("ARB_MASTER_DB")
    if p and os.path.exists(p):
        return p
    for rel in ("../../lofarb-master/database/arb_master.db",
                "../../LOFarb/database/arb_master.db"):
        cand = os.path.abspath(os.path.join(HERE, rel))
        if os.path.exists(cand):
            return cand
    raise SystemExit("找不到 arb_master.db，请设置 ARB_MASTER_DB")


def main():
    apply = "--apply" in sys.argv
    db = db_path()
    print("DB =", db)
    c = sqlite3.connect(db)

    existing = {r[0] for r in c.execute("SELECT fund_code FROM fund_profile")}
    todo = [x for x in CODES if x[0] not in existing]
    skip = [x for x in CODES if x[0] in existing]

    print("\n已在库中（跳过）%d 只: %s" % (len(skip), [x[0] for x in skip]))
    print("待新增 %d 只:" % len(todo))
    for code, name, idx, note in todo:
        print("  %s  %-14s  %-14s  %s" % (code, name, idx, note))

    if not todo:
        print("\n无需新增。")
        return
    if not apply:
        print("\n[dry-run] 加 --apply 才会写入。")
        return

    n = 0
    for code, name, idx, note in todo:
        c.execute(
            "INSERT INTO fund_profile "
            "(fund_code, jsl_market, fund_name, fund_type, category, "
            " idx_name, jsl_index_nm, val_method, in_arbtest, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
            (code, "C", name, "LOF", "现金管理", idx, idx, "pending", 0))
        n += 1
    c.commit()
    print("\n✅ 已写入 %d 只。" % n)

    print("\n--- 写入后 C 市场清单 ---")
    for r in c.execute("SELECT fund_code, fund_name, val_method FROM fund_profile "
                       "WHERE jsl_market='C' ORDER BY fund_code"):
        print("  %s  %-16s  %s" % r)
    c.close()


if __name__ == "__main__":
    main()
