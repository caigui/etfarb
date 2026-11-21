# -*- coding: utf-8 -*-
"""新增 2 只可转债 ETF 到 fund_profile（现金管理 TAB，估值已接入）。

用法：
    python core/_add_cb2.py            # dry-run，打印将要写入的内容
    python core/_add_cb2.py --apply    # 真正写入

口径说明（2026-11-17 晚，请求 H：东哥「511380 511180 放到现金管理里面」）：
  · jsl_market='C'  → 出现在看板「现金管理」TAB（与现有 13 只同口径）
  · category='现金管理'
  · val_method='bond' → 估值**已接入**，直接走 core/bond_cb_etf_valuation.py。
        不像请求 F 那样先给 'pending' 留空 —— 这次算法先写完验证通过了才入库。
  · fund_type='LOF' 与 C 市场其余标的一致（ETFarb 用 LOF 分支做估值分流）
  · idx_code 留空：这两只的驱动指数走 bond_cb_etf_valuation 内部映射，
        idx_provider 那套（免费指数源）不适用可转债指数。
"""
from __future__ import print_function

import os
import sqlite3
import sys

CODES = [
    # (代码, 名称, 跟踪指数展示名, 备注)
    ("511380", "可转债ETF博时",   "中证可转债及可交换债券指数(931078)", "沪市·成份302只"),
    ("511180", "可转债ETF海富通", "上证可转换债券指数(000139)",         "沪市·成份85只"),
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
    print("DB:", db, "(apply)" if apply else "(dry-run)")
    c = sqlite3.connect(db)
    try:
        cols = [d[1] for d in c.execute("PRAGMA table_info(fund_profile)")]
        for code, name, idxname, note in CODES:
            exist = c.execute("SELECT fund_code, fund_name, val_method, category "
                              "FROM fund_profile WHERE fund_code=?",
                              (code,)).fetchone()
            if exist:
                print("  跳过（已存在）:", exist)
                continue
            row = {"fund_code": code, "fund_name": name, "jsl_market": "C",
                   "category": "现金管理", "fund_type": "LOF",
                   "val_method": "bond", "jsl_index_nm": idxname}
            row = {k: v for k, v in row.items() if k in cols}
            print("  新增 %s %s  %s  market=C category=现金管理 val_method=bond"
                  % (code, name, note))
            if apply:
                ks = list(row)
                c.execute("INSERT INTO fund_profile (%s) VALUES (%s)"
                          % (",".join(ks), ",".join("?" * len(ks))),
                          [row[k] for k in ks])
        if apply:
            c.commit()
            print("已提交。")
            for code, *_ in CODES:
                print("  ", c.execute(
                    "SELECT fund_code,fund_name,jsl_market,category,val_method "
                    "FROM fund_profile WHERE fund_code=?", (code,)).fetchone())
        else:
            print("（dry-run，未写入；加 --apply 生效）")
    finally:
        c.close()


if __name__ == "__main__":
    main()