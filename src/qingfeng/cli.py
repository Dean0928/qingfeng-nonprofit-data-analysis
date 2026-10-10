"""命令行入口。

::

    qingfeng data      # 只生成模拟数据
    qingfeng pipeline  # 端到端跑通：清洗 -> 校验 -> 建模 -> 分析 -> 报告
    qingfeng verify    # 只读体检：检查产物是否齐备、外键是否自洽
    qingfeng sql       # 执行 sql/ 下的查询并打印结果
    qingfeng info      # 打印当前配置与数据窗口

也可以用 ``python -m qingfeng.cli <子命令>`` 调用。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from qingfeng import __version__
from qingfeng.config import ConfigError, load_config
from qingfeng.logging_utils import setup_logging
from qingfeng.pipeline import generate_data, run_pipeline, verify_pipeline
from qingfeng.warehouse import all_sql_files, list_tables, query, run_sql_file, table_stats


def _print_df(df: pd.DataFrame, max_rows: int = 30) -> None:
    if df is None or not len(df):
        print("（无结果）")
        return
    with pd.option_context("display.max_rows", max_rows, "display.max_columns", 40,
                           "display.width", 200, "display.max_colwidth", 40):
        print(df.head(max_rows).to_string(index=False))


def cmd_data(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, ensure_dirs=True)
    generate_data(cfg, seed=args.seed)
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, ensure_dirs=True)
    run_pipeline(cfg, regenerate=not args.no_data, seed=args.seed, skip_figures=args.skip_figures)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    result = verify_pipeline(cfg)
    return 0 if result["ok"] or args.lenient else 1


def cmd_sql(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.file:
        results = run_sql_file(cfg, args.file)
        for label, df in results:
            print(f"\n{'=' * 78}\n-- {label}\n{'=' * 78}")
            _print_df(df)
        return 0
    if args.query:
        _print_df(query(cfg, args.query))
        return 0
    print("可用的 SQL 文件：")
    for p in all_sql_files(cfg):
        print(f"  {p.relative_to(cfg.paths.root)}")
    print("\n数据仓库中的表：")
    _print_df(table_stats(cfg))
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print(f"机构            : {cfg.org_name}")
    print(f"地区            : {cfg.region}")
    print(f"配置文件        : {cfg.source_path}")
    print(f"仓库根目录      : {cfg.paths.root}")
    print(f"随机种子        : {cfg.simulation.seed}")
    print(f"数据窗口        : {cfg.simulation.start_date} ~ {cfg.simulation.as_of_date}")
    print(f"服务价值折算率  : {cfg.simulation.volunteer_hour_value_cny} 元/小时")
    print(f"数据仓库        : {cfg.paths.warehouse}")
    if cfg.paths.warehouse.is_file():
        tables = list_tables(cfg, "table") + list_tables(cfg, "view")
        print(f"仓库对象        : {len(tables)} 个 -> {', '.join(tables[:12])}"
              + (" ..." if len(tables) > 12 else ""))
    else:
        print("仓库对象        : （尚未建库，请运行 `qingfeng pipeline`）")
    print(f"SQL 脚本        : {len(list(all_sql_files(cfg)))} 个")
    print(f"图表            : {len(list(cfg.paths.figures.glob('*.png')))} 张")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qingfeng",
        description="迪庆清风青年公益联合会 · 公益项目数据运营分析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"qingfeng-analytics {__version__}")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/config.yaml）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 级日志")
    sub = parser.add_subparsers(dest="command", required=True)

    p_data = sub.add_parser("data", help="只生成模拟多源数据")
    p_data.add_argument("--seed", type=int, default=None, help="覆盖随机种子")
    p_data.set_defaults(func=cmd_data)

    p_pipe = sub.add_parser("pipeline", help="端到端运行完整分析管道")
    p_pipe.add_argument("--seed", type=int, default=None, help="覆盖随机种子")
    p_pipe.add_argument("--no-data", action="store_true", help="跳过数据生成，复用已有 raw 数据")
    p_pipe.add_argument("--skip-figures", action="store_true", help="跳过图表生成（加快 CI）")
    p_pipe.set_defaults(func=cmd_pipeline)

    p_verify = sub.add_parser("verify", help="只读体检已生成的产物")
    p_verify.add_argument("--lenient", action="store_true", help="存在失败项时也返回 0")
    p_verify.set_defaults(func=cmd_verify)

    p_sql = sub.add_parser("sql", help="执行 sql/ 下的查询")
    p_sql.add_argument("--file", default=None, help="SQL 文件（相对 sql/ 或绝对路径）")
    p_sql.add_argument("--query", default=None, help="直接执行一条 SQL")
    p_sql.set_defaults(func=cmd_sql)

    p_info = sub.add_parser("info", help="打印配置与产物概况")
    p_info.set_defaults(func=cmd_info)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging("DEBUG" if args.verbose else "INFO")
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"缺少文件：{exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:  # pragma: no cover
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
