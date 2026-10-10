#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从实际产物生成数据字典文档。

数据字典是数据分析项目里最容易被忽略、却最影响协作效率的交付物。与其手写（必然过时），
不如让管道每次运行时从**真实的数据表**导出：

* 字段名、数据类型、非空率、基数（唯一值个数）
* 取值范围 / 样例值
* 该字段参与的数据质量规则
* 该字段被哪些指标口径引用

产出：``docs/data_dictionary.md``

用法::

    python scripts/build_docs.py            # 依赖已生成的 data/processed 与 data/raw
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qingfeng.config import load_config  # noqa: E402
from qingfeng.logging_utils import get_logger  # noqa: E402
from qingfeng.tables import TABLE_SPECS  # noqa: E402
from qingfeng.transform import load_star_schema  # noqa: E402

logger = get_logger("build_docs")

MAX_SAMPLE_VALUES = 4


def _describe_column(s: pd.Series) -> dict[str, object]:
    n = len(s)
    non_null = int(s.notna().sum())
    nunique = int(s.nunique(dropna=True))
    dtype = str(s.dtype)
    if pd.api.types.is_numeric_dtype(s) and non_null:
        v = pd.to_numeric(s, errors="coerce").dropna()
        if len(v):
            rng = f"[{_short(v.min())}, {_short(v.max())}]  中位数 {_short(v.median())}"
        else:
            rng = "—"
        sample = ""
    elif pd.api.types.is_datetime64_any_dtype(s) and non_null:
        rng = f"[{s.min()}, {s.max()}]"
        sample = ""
    else:
        vc = s.dropna().astype(str).value_counts()
        rng = f"{nunique} 个不同取值" if nunique else "—"
        sample = "；".join(vc.index[:MAX_SAMPLE_VALUES].tolist())
    return {
        "字段名": "",
        "类型": dtype,
        "非空率": round(non_null / n, 4) if n else 0.0,
        "基数": nunique,
        "取值范围/分布": rng,
        "高频取值": sample,
    }


def _short(v: object) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f) >= 1e6:
        return f"{f / 1e4:,.0f}万"
    if abs(f) >= 1000:
        return f"{f:,.0f}"
    if float(f).is_integer():
        return str(int(f))
    return f"{f:.3f}"


def build_data_dictionary(cfg) -> Path:
    star = load_star_schema(cfg)
    validation_path = cfg.paths.processed_file("validation_report.csv")
    validation = pd.read_csv(validation_path, encoding="utf-8-sig") if validation_path.is_file() else pd.DataFrame()

    lines: list[str] = []
    add = lines.append
    add("# 数据字典\n")
    add(
        "> 本文件由 `scripts/build_docs.py` 从**实际生成的数据**自动导出，"
        "请勿手工修改（改字段请改 `src/qingfeng/tables.py` 与生成器脚本后重跑 `make pipeline`）。\n"
    )
    add(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M} ｜ 数据窗口："
        f"{cfg.simulation.start_date} ~ {cfg.simulation.as_of_date} ｜ 随机种子：{cfg.simulation.seed}\n")

    add("\n## 一、数据源总览\n")
    add("| 数据表 | 中文名称 | 来源系统 | 粒度 | 主键 |")
    add("|---|---|---|---|---|")
    for name, spec in TABLE_SPECS.items():
        add(f"| `{name}` | {spec['zh_name']} | {spec['source']} | {spec['grain']} | "
            f"`{spec.get('primary_key') or '—'}` |")

    add("\n## 二、星型模型\n")
    add("| 表名 | 类型 | 行数 | 列数 | 说明 |")
    add("|---|---|---|---|---|")
    for name, df in star.items():
        kind = "维度表" if name.startswith("dim_") else "事实表"
        spec_key = name.replace("dim_", "").replace("fact_", "")
        note = TABLE_SPECS.get(spec_key, {}).get("grain", "")
        if name == "dim_date":
            note = "一行 = 一个自然日（日期维度，覆盖全部事实表的日期范围）"
        if name == "dim_geography":
            note = "一行 = 一个「县 + 乡镇」组合（复合业务键 geo_code）"
        add(f"| `{name}` | {kind} | {len(df):,} | {df.shape[1]} | {note} |")

    add(
        "\n> **UNKNOWN 成员约定**：每张维度表都有一行代理键为 `-1` 的记录，用于承载源系统中"
        "缺失或悬空的维度引用。事实表按维度汇总时，未匹配的量会落在该成员上而不会被静默丢弃。\n"
    )

    add("\n## 三、字段明细\n")
    for name, df in star.items():
        add(f"\n### 3.{list(star).index(name) + 1} `{name}`\n")
        rows = []
        for col in df.columns:
            d = _describe_column(df[col])
            d["字段名"] = col
            rows.append(d)
        tbl = pd.DataFrame(rows)[["字段名", "类型", "非空率", "基数", "取值范围/分布", "高频取值"]]
        add(_md(tbl))
        if len(validation):
            rules = validation[validation["table"].astype(str).str.contains(
                name.replace("dim_", "").replace("fact_", ""), regex=False, na=False
            )]
            if len(rules):
                add(f"\n**参与的质量规则（{len(rules)} 条）**："
                    + "；".join(f"`{r}`" for r in rules["rule"].head(12)))
                add("")

    add("\n## 四、指标口径索引\n")
    add("核心指标的口径公式、目标值与数据来源见 `reports/kpi_catalog.csv`。\n")
    add("常用的口径定义速查：\n")
    add("| 概念 | 口径 |")
    add("|---|---|")
    add("| 活跃志愿者 | 在观察窗口内至少产生 1 条志愿服务签到记录的志愿者 |")
    add("| 志愿者活跃率 | 活跃志愿者数 ÷ 注册志愿者总数（全国基线 28.72%） |")
    add("| 志愿服务经济价值 | 服务时长 × 30 元/小时（由全国口径反推的折算率） |")
    add("| 服务人次 | `fact_beneficiary_service` 的记录数（去重后的一次服务 = 一行） |")
    add("| 复访率 | 服务 ≥2 次的服务对象数 ÷ 有服务记录的服务对象数 |")
    add("| 预算执行率 | SUM(实际支出) ÷ SUM(计划支出) |")
    add("| 捐赠来源集中度 HHI | SUM((单个捐赠人金额 ÷ 捐赠总额)²) |")
    add("| 签约有效期留存 | 注册满 12 个月且近 6 个月仍有服务记录的志愿者占比 |")
    add("| 数据完整性 | 1 − 缺失单元格数 ÷ 总单元格数 |")

    add("\n## 五、已知局限\n")
    add(
        "1. `fact_volunteer_service` 本身不含乡镇字段，`geo_key` 由项目台账的乡镇反推，"
        "因此无法刻画同一项目跨乡镇的服务分布。\n"
        "2. `dim_volunteer` 中少量无任何服务记录的志愿者其 `join_date` 无法反推，保留为空，"
        "这类记录仍计入注册志愿者总数，但不参与在职时长类指标。\n"
        "3. `fact_in_kind` 中入库日期缺失的记录已从事实表剔除（原因见 `cleaning_log.csv`），"
        "因此物资捐赠总额会略低于源表直接求和的结果。\n"
        "4. 满意度评分缺失的记录不参与满意度均值，但计入服务人次。\n"
    )

    out = cfg.paths.root / "docs" / "data_dictionary.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("数据字典已生成：%s（%d 张表）", out, len(star))
    return out


def _md(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in df.iterrows():
        out.append("| " + " | ".join(str(v).replace("|", "\\|") for v in row) + " |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="从实际产物生成数据字典")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args(argv)
    cfg = load_config(args.config, ensure_dirs=True)
    build_data_dictionary(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
