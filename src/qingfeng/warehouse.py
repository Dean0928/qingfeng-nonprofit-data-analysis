"""SQLite 数据仓库：把星型模型装库，并提供 SQL 查询接口。

用 SQLite 而不是重型数据库的理由：整个仓库 ``git clone`` 后无需任何外部服务即可
复现；同时它完整支持窗口函数、CTE、``EXPLAIN QUERY PLAN``，足以演示数据仓库建模、
索引设计与 SQL 分析能力。切换 DuckDB / PostgreSQL 只需替换连接串。

用法::

    from qingfeng.warehouse import build_warehouse, query
    build_warehouse(cfg)
    query(cfg, "SELECT * FROM v_project_kpi LIMIT 5")
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

import pandas as pd

from qingfeng.config import Config
from qingfeng.logging_utils import get_logger
from qingfeng.transform import STAR_TABLES, load_star_schema

logger = get_logger("warehouse")

# 建库后补建的索引：覆盖全部外键与主要时间键
INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_fvs_date ON fact_volunteer_service(date_key)",
    "CREATE INDEX IF NOT EXISTS ix_fvs_vol ON fact_volunteer_service(volunteer_key)",
    "CREATE INDEX IF NOT EXISTS ix_fvs_proj ON fact_volunteer_service(project_key)",
    "CREATE INDEX IF NOT EXISTS ix_fvs_geo ON fact_volunteer_service(geo_key)",
    "CREATE INDEX IF NOT EXISTS ix_fd_date ON fact_donation(date_key)",
    "CREATE INDEX IF NOT EXISTS ix_fd_donor ON fact_donation(donor_key)",
    "CREATE INDEX IF NOT EXISTS ix_fd_proj ON fact_donation(project_key)",
    "CREATE INDEX IF NOT EXISTS ix_fd_ym ON fact_donation(year_month)",
    "CREATE INDEX IF NOT EXISTS ix_fik_date ON fact_in_kind(date_key)",
    "CREATE INDEX IF NOT EXISTS ix_fik_donor ON fact_in_kind(donor_key)",
    "CREATE INDEX IF NOT EXISTS ix_fik_proj ON fact_in_kind(project_key)",
    "CREATE INDEX IF NOT EXISTS ix_fbs_date ON fact_beneficiary_service(date_key)",
    "CREATE INDEX IF NOT EXISTS ix_fbs_bene ON fact_beneficiary_service(beneficiary_key)",
    "CREATE INDEX IF NOT EXISTS ix_fbs_proj ON fact_beneficiary_service(project_key)",
    "CREATE INDEX IF NOT EXISTS ix_fbs_geo ON fact_beneficiary_service(geo_key)",
    "CREATE INDEX IF NOT EXISTS ix_fpm_proj ON fact_project_monthly(project_key)",
    "CREATE INDEX IF NOT EXISTS ix_fpm_ym ON fact_project_monthly(year_month)",
    "CREATE INDEX IF NOT EXISTS ix_vol_county ON dim_volunteer(county)",
    "CREATE INDEX IF NOT EXISTS ix_proj_county ON dim_project(county)",
)


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.paths.warehouse.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(cfg.paths.warehouse)


def build_warehouse(cfg: Config, star: dict[str, pd.DataFrame] | None = None) -> Path:
    """把星型模型写入 SQLite，并执行 ``sql/`` 下的 DDL 与视图脚本。

    顺序很重要：
    1. 用 ``to_sql`` 建表并灌数（保证与 CSV 逐行一致）；
    2. 执行 ``sql/01_schema.sql`` 中的 ``CREATE INDEX``（DDL 里的 ``DROP/CREATE TABLE``
       会被跳过，避免清空刚写入的数据）；
    3. 执行 ``sql/02_load_views.sql`` 建立分析视图。
    """
    star = star or load_star_schema(cfg)
    cfg.paths.warehouse.parent.mkdir(parents=True, exist_ok=True)

    with connect(cfg) as con:
        for name in STAR_TABLES:
            if name not in star:
                continue
            star[name].to_sql(name, con, if_exists="replace", index=False)
            logger.info("  装入 %-28s %7d 行", name, len(star[name]))

        for stmt in INDEX_STATEMENTS:
            con.execute(stmt)
        con.commit()
        logger.info("  已建立 %d 个索引", len(INDEX_STATEMENTS))

        # DDL 文件中的索引语句（跳过会破坏数据的 DROP/CREATE TABLE）
        schema_file = cfg.paths.sql / "01_schema.sql"
        if schema_file.is_file():
            n = _exec_index_only(con, schema_file.read_text(encoding="utf-8"))
            logger.info("  由 01_schema.sql 追加执行 %d 条索引语句", n)

        # 安全护栏：DDL 脚本一旦误执行 DROP/CREATE TABLE，会静默清空刚装入的数据。
        # 这里显式校验所有星型表都存在且非空，让小概率的脚本事故立即暴露。
        present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in STAR_TABLES if t not in present]
        if missing:
            raise RuntimeError(
                f"建库异常：以下星型表在装入后消失 {missing}；"
                "请检查 sql/01_schema.sql 是否被误执行了 DROP TABLE。"
            )
        empty = [t for t in STAR_TABLES if con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] == 0]
        if empty:
            raise RuntimeError(f"建库异常：以下星型表为空 {empty}")

        views_file = cfg.paths.sql / "02_load_views.sql"
        if views_file.is_file():
            allowed = ("CREATE VIEW", "CREATE OR REPLACE VIEW", "DROP VIEW", "CREATE TEMP VIEW")
            n_views = 0
            for _label, stmt in split_sql_statements(views_file.read_text(encoding="utf-8")):
                head = stmt.upper().lstrip()
                if not head.startswith(allowed):
                    logger.debug("视图脚本中跳过非视图语句：%s", stmt[:60])
                    continue
                try:
                    con.execute(stmt)
                    n_views += 1
                except sqlite3.Error as exc:
                    logger.warning("  视图语句执行失败：%s（%s）", stmt[:60], exc)
            con.commit()
            logger.info("  已由 02_load_views.sql 执行 %d 条视图语句", n_views)

    logger.info("仓库已生成：%s", cfg.paths.warehouse)
    return cfg.paths.warehouse


def _exec_index_only(con: sqlite3.Connection, script: str) -> int:
    """只执行脚本中的 ``CREATE INDEX`` 语句。"""
    executed = 0
    for _label, stmt in split_sql_statements(script):
        if stmt.upper().lstrip().startswith("CREATE INDEX"):
            try:
                con.execute(stmt)
                executed += 1
            except sqlite3.Error as exc:  # pragma: no cover - 索引重复/语法问题时给出提示
                logger.debug("索引语句跳过：%s（%s）", stmt[:60], exc)
    con.commit()
    return executed


def split_sql_statements(text: str) -> list[tuple[str, str]]:
    """把 SQL 脚本切分为 [(标签, 语句), ...]。

    规则：
    * 以 ``--`` 开头的行视为注释；连续注释块作为紧随其后的语句标签，
      标签优先取含「指标：」或「分析：」的那一行。
    * 语句以分号结束；分号之间的多行内容属于同一条语句。
    * 分节横幅（``####...``）与分隔线会被忽略，不污染标签。
    """
    statements: list[tuple[str, str]] = []
    label_buffer: list[str] = []
    buffer: list[str] = []

    def _flush() -> None:
        nonlocal buffer, label_buffer
        stmt = "\n".join(buffer).strip()
        if not stmt:
            buffer, label_buffer = [], []
            return
        meaningful = [ln for ln in label_buffer if ln and not set(ln) <= set("#=- ")]
        preferred = [ln for ln in meaningful if ("指标：" in ln or "分析：" in ln)]
        label = (preferred or meaningful or [" ".join(stmt.split())[:60]])[-1]
        statements.append((label, stmt))
        buffer, label_buffer = [], []

    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("--"):
            if not buffer:
                label_buffer.append(stripped.lstrip("-").strip())
            continue
        if not stripped:
            continue
        buffer.append(raw)
        if stripped.endswith(";"):
            _flush()
    _flush()
    return statements


def resolve_sql_path(cfg: Config, path: str | Path) -> Path:
    """把用户给的 SQL 路径解析为真实文件。

    ``--file`` 参数既可能写成 ``03_kpi_queries.sql``（相对 ``sql/``），
    也可能写成 ``sql/03_kpi_queries.sql``（相对仓库根目录）。
    只做一次简单拼接会把后者解析成 ``sql/sql/...`` 而报"文件不存在"，
    因此这里按候选顺序逐个探测，全部失败时才报错。
    """
    p = Path(path)
    if p.is_absolute():
        if p.is_file():
            return p
        raise FileNotFoundError(f"SQL 文件不存在：{p}")
    for candidate in (cfg.paths.sql / p, cfg.paths.root / p, Path.cwd() / p):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"找不到 SQL 文件 {path}；已尝试："
        + ", ".join(str(c) for c in (cfg.paths.sql / p, cfg.paths.root / p, Path.cwd() / p))
    )


def run_sql_file(cfg: Config, path: str | Path) -> list[tuple[str, pd.DataFrame]]:
    """执行一个 ``.sql`` 文件，返回 [(语句标签, 结果表), ...]。"""
    p = resolve_sql_path(cfg, path)
    text = p.read_text(encoding="utf-8")

    results: list[tuple[str, pd.DataFrame]] = []
    with connect(cfg) as con:
        for label, stmt in split_sql_statements(text):
            try:
                df = pd.read_sql_query(stmt, con)
            except Exception as exc:  # noqa: BLE001 - 验证脚本需要继续跑完
                results.append((label, pd.DataFrame({"error": [str(exc)]})))
                continue
            results.append((label, df))
    return results


def query(cfg: Config, sql: str, params: Sequence | dict | None = None) -> pd.DataFrame:
    """执行单条查询并返回 DataFrame。"""
    with connect(cfg) as con:
        return pd.read_sql_query(sql, con, params=params)


def list_tables(cfg: Config, kind: str = "table") -> list[str]:
    sql = "SELECT name FROM sqlite_master WHERE type = ? ORDER BY name"
    df = query(cfg, sql, params=(kind,))
    return df["name"].tolist() if len(df) else []


def table_stats(cfg: Config) -> pd.DataFrame:
    """各表行数与列数。"""
    rows = []
    with connect(cfg) as con:
        for name in list_tables(cfg, "table"):
            n = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            cols = len(con.execute(f'PRAGMA table_info("{name}")').fetchall())
            rows.append({"table": name, "rows": int(n), "columns": cols})
    return pd.DataFrame(rows).sort_values("rows", ascending=False).reset_index(drop=True)


def explain(cfg: Config, sql: str) -> pd.DataFrame:
    """``EXPLAIN QUERY PLAN``：用于在报告中展示索引是否生效。"""
    with connect(cfg) as con:
        return pd.read_sql_query(f"EXPLAIN QUERY PLAN {sql}", con)


def integrity_check(cfg: Config) -> list[str]:
    """外键一致性体检（在事实表与维度表之间做 LEFT JOIN 计数）。"""
    checks: list[tuple[str, str]] = [
        ("fact_volunteer_service->dim_volunteer", "volunteer_key"),
        ("fact_volunteer_service->dim_project", "project_key"),
        ("fact_donation->dim_donor", "donor_key"),
        ("fact_donation->dim_project", "project_key"),
        ("fact_beneficiary_service->dim_beneficiary", "beneficiary_key"),
        ("fact_beneficiary_service->dim_project", "project_key"),
        ("fact_project_monthly->dim_project", "project_key"),
    ]
    issues: list[str] = []
    with connect(cfg) as con:
        for spec, key in checks:
            fact, dim = spec.split("->")
            parent = dim.replace("dim_", "dim_")
            n = con.execute(
                f'SELECT COUNT(*) FROM "{fact}" f LEFT JOIN "{parent}" d '
                f'ON f."{key}" = d."{key}" WHERE d."{key}" IS NULL'
            ).fetchone()[0]
            if n:
                issues.append(f"{spec}: {n} 行外键无法匹配")
    return issues


def all_sql_files(cfg: Config) -> Iterable[Path]:
    return sorted(p for p in cfg.paths.sql.glob("*.sql"))
