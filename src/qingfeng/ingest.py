"""多源异构建入层。

职责边界：**只负责把数据读进来并核对完整性，不做任何内容修改**。
清洗（`clean.py`）才允许改动数值。

为什么单独一层
--------------
基层公益组织的数据几乎不可能来自一个系统。把"接入"与"清洗"分开有两个好处：

1. 接入层可以独立核对**每次数据交接是否完整**（行数、摘要与清单是否一致），
   这是数据运营岗最日常、也最容易被跳过的一步；
2. 清洗层可以放心地修改数据，因为它知道"原始数据已被取证并核对过"。

清单文件 ``data/raw/_MANIFEST.json`` 由生成器写出，记录了每张表的行数、
列数、字节数与 SHA-256 摘要，接入层据此做对账。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from qingfeng.config import Config
from qingfeng.logging_utils import get_logger
from qingfeng.tables import ALL_TABLES, TABLE_SPECS

logger = get_logger("ingest")

MANIFEST_NAME = "_MANIFEST.json"
GROUND_TRUTH_DIR = "_ground_truth"
CSV_ENCODING = "utf-8-sig"  # 源系统导出（Excel / 问卷星）普遍带 BOM


@dataclass(frozen=True)
class SourceRecord:
    """一个数据源的元信息。"""

    table: str
    zh_name: str
    source_system: str
    grain: str
    path: Path
    rows: int | None = None
    columns: int | None = None
    sha256_16: str | None = None


# ---------------------------------------------------------------------------
# 发现与读取
# ---------------------------------------------------------------------------


def read_manifest(cfg: Config) -> dict[str, Any]:
    """读取生成器写出的数据清单；不存在时返回空字典（允许使用外部真实数据）。"""
    path = cfg.paths.raw / MANIFEST_NAME
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def list_sources(cfg: Config) -> list[SourceRecord]:
    """枚举原始数据目录下的全部数据源，并与清单对账。"""
    manifest = read_manifest(cfg)
    by_table = {t["table"]: t for t in manifest.get("tables", [])}
    records: list[SourceRecord] = []
    for table in ALL_TABLES:
        path = cfg.paths.raw_file(f"{table}.csv")
        if not path.is_file():
            continue
        spec = TABLE_SPECS[table]
        meta = by_table.get(table, {})
        records.append(
            SourceRecord(
                table=table,
                zh_name=spec["zh_name"],
                source_system=spec["source"],
                grain=spec["grain"],
                path=path,
                rows=meta.get("rows"),
                columns=meta.get("columns"),
                sha256_16=meta.get("sha256_16"),
            )
        )
    return records


def read_raw_table(cfg: Config, name: str) -> pd.DataFrame:
    """读取单张原始表。"""
    path = cfg.paths.raw_file(f"{name}.csv")
    if not path.is_file():
        raise FileNotFoundError(
            f"缺少原始数据 {path}；请先运行 `make data`（生成模拟数据）"
            "或把真实数据按 data/raw 的命名放入。"
        )
    return pd.read_csv(path, encoding=CSV_ENCODING, low_memory=False)


def load_all(cfg: Config) -> dict[str, pd.DataFrame]:
    """读取全部原始表。"""
    tables: dict[str, pd.DataFrame] = {}
    for record in list_sources(cfg):
        tables[record.table] = read_raw_table(cfg, record.table)
        logger.info("  读入 %-28s %7d 行 x %2d 列", record.table, len(tables[record.table]),
                    tables[record.table].shape[1])
    missing = set(ALL_TABLES) - set(tables)
    if missing:
        logger.warning("以下数据源缺失，将被跳过：%s", sorted(missing))
    return tables


def read_ground_truth(cfg: Config, name: str = "injected_defects") -> pd.DataFrame:
    """读取注入缺陷 / 植入异常的 ground truth 清单（用于验证清洗与检测效果）。"""
    path = cfg.paths.raw / GROUND_TRUTH_DIR / f"{name}.csv"
    if not path.is_file():
        return pd.DataFrame()
    return pd.read_csv(path, encoding=CSV_ENCODING)


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------


def _sha256_16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def verify_manifest(cfg: Config) -> pd.DataFrame:
    """把当前原始数据与清单逐表对账，返回差异表。

    检查项：文件存在、行数一致、SHA-256 摘要一致。
    任何一项不一致都说明"数据在交接后被改动过"，需要先查清原因再下游分析。
    """
    manifest = read_manifest(cfg)
    if not manifest:
        return pd.DataFrame(columns=["table", "检查项", "清单值", "实际值", "结果"])

    rows: list[dict[str, Any]] = []
    for entry in manifest.get("tables", []):
        table = entry["table"]
        path = cfg.paths.raw_file(f"{table}.csv")
        if not path.is_file():
            rows.append({"table": table, "检查项": "文件存在", "清单值": "存在",
                         "实际值": "缺失", "结果": "FAIL"})
            continue
        digest = _sha256_16(path)
        rows.append({"table": table, "检查项": "SHA-256", "清单值": entry["sha256_16"],
                     "实际值": digest, "结果": "PASS" if digest == entry["sha256_16"] else "FAIL"})
        n_rows = sum(1 for _ in path.open(encoding=CSV_ENCODING)) - 1
        rows.append({"table": table, "检查项": "行数", "清单值": entry["rows"],
                     "实际值": n_rows, "结果": "PASS" if n_rows == entry["rows"] else "FAIL"})
    return pd.DataFrame(rows)


def source_summary(cfg: Config) -> pd.DataFrame:
    """数据源清单（供报告与数据字典引用）。"""
    rows = []
    for r in list_sources(cfg):
        actual_rows = r.rows
        if actual_rows is None:
            actual_rows = sum(1 for _ in r.path.open(encoding=CSV_ENCODING)) - 1
        rows.append(
            {
                "数据表": r.table,
                "中文名称": r.zh_name,
                "来源系统": r.source_system,
                "粒度": r.grain,
                "行数": actual_rows,
                "文件大小(KB)": round(r.path.stat().st_size / 1024, 1),
            }
        )
    return pd.DataFrame(rows)
