#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQL 层验证：逐条执行 ``sql/*.sql`` 并产出可提交的验证日志。

为什么需要它
------------
SQL 脚本很容易"看起来对但跑不起来"（缺列、函数不存在、窗口函数语法在 SQLite 下不被支持）。
本脚本把每个语句都真正执行一遍，记录 PASS/FAIL 与返回行数，输出到
``reports/sql_verification.log``，并让 CI 在任一语句失败时终止。

用法::

    python scripts/verify_sql.py                 # 验证全部 SQL 文件
    python scripts/verify_sql.py --file 03_kpi_queries.sql
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from qingfeng.config import load_config  # noqa: E402
from qingfeng.warehouse import connect, split_sql_statements, table_stats  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def verify(cfg, files: list[Path], log_path: Path) -> bool:
    lines: list[str] = []
    add = lines.append
    add("=" * 90)
    add("SQL 层验证日志")
    add(f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    add(f"数据库：{cfg.paths.warehouse}")
    add(f"随机种子：{cfg.simulation.seed}　数据窗口：{cfg.simulation.start_date} ~ {cfg.simulation.as_of_date}")
    add("=" * 90)

    with connect(cfg) as con:
        stats = table_stats(cfg)
        add("")
        add("【数据仓库对象】")
        for _, r in stats.iterrows():
            add(f"  {r['table']:<32} {r['rows']:>8,} 行  {r['columns']:>2} 列")
        views = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name", con
        )["name"].tolist()
        add(f"  视图 {len(views)} 个：{', '.join(views)}")

    total = passed = 0
    failures: list[str] = []
    for path in files:
        add("")
        add("#" * 90)
        add(f"# 文件：{path.name}")
        add("#" * 90)
        stmts = split_sql_statements(path.read_text(encoding="utf-8"))
        for idx, (label, stmt) in enumerate(stmts, start=1):
            total += 1
            title = label.splitlines()[0] if label else f"语句 {idx}"
            head_kw = " ".join(stmt.split())[:8].upper()
            is_query = head_kw.startswith(("SELECT", "WITH", "PRAGMA", "EXPLAIN"))
            with connect(cfg) as con:
                try:
                    if is_query:
                        df = pd.read_sql_query(stmt, con)
                    else:
                        # DDL / DML：执行但不返回结果集（重复执行用 IF NOT EXISTS 保证幂等）
                        con.execute(stmt)
                        df = None
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{path.name} #{idx} {title}: {exc}")
                    add("")
                    add(f"[FAIL] #{idx} {title}")
                    add(f"       错误：{exc}")
                    add("       语句：" + " ".join(stmt.split())[:300])
                    continue
            passed += 1
            add("")
            if df is None:
                add(f"[PASS] #{idx} {title}　→ DDL/DML 执行成功（无结果集）")
            else:
                add(f"[PASS] #{idx} {title}　→ 返回 {len(df)} 行 × {df.shape[1]} 列")
                if len(df):
                    head = df.head(2).to_string(index=False, max_colwidth=26)
                    for line in head.splitlines():
                        add("       " + line)

    add("")
    add("=" * 90)
    add(f"汇总：{passed}/{total} 条语句执行成功")
    if failures:
        add("失败明细：")
        for f in failures:
            add("  - " + f)
    add("结论：" + ("全部通过" if not failures else "存在失败语句，需修复"))
    add("=" * 90)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"SQL 验证：{passed}/{total} 通过 -> {log_path}")
    if failures:
        for f in failures:
            print("  FAIL:", f)
    return not failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证 sql/ 下的全部查询")
    parser.add_argument("--config", default=None)
    parser.add_argument("--file", action="append", default=None, help="只验证指定文件（可重复）")
    parser.add_argument("--log", default="reports/sql_verification.log")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if not cfg.paths.warehouse.is_file():
        print(f"缺少数据仓库 {cfg.paths.warehouse}，请先运行 make pipeline", file=sys.stderr)
        return 2
    files = (
        [Path(f) if Path(f).is_absolute() else cfg.paths.sql / f for f in args.file]
        if args.file
        else sorted(cfg.paths.sql.glob("03_*.sql")) + sorted(cfg.paths.sql.glob("04_*.sql"))
    )
    ok = verify(cfg, [f for f in files if f.is_file()], cfg.paths.root / args.log)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
