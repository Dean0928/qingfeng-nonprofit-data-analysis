"""端到端管道编排。

一条命令完成：生成数据 → 清洗 → 校验 → 星型模型 → 建库 → 指标 → 分析 →
异常检测 → 图表 → 报告。

每一步都被 :class:`~qingfeng.logging_utils.RunTimer` 计时并记录，运行结束打印阶段耗时表，
便于在 CI 里发现性能退化。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from qingfeng.analyze import run_analysis, write_analysis
from qingfeng.anomaly import run_anomaly_detection, write_anomaly_results
from qingfeng.clean import CleaningLog, load_cleaned, run_cleaning, write_cleaned
from qingfeng.config import Config, load_config
from qingfeng.ingest import source_summary, verify_manifest
from qingfeng.logging_utils import RunTimer, get_logger
from qingfeng.metrics import compute_kpis, dimension_breakdown, kpi_timeseries
from qingfeng.report import write_reports
from qingfeng.transform import (
    build_star_schema,
    load_star_schema,
    star_schema_summary,
    write_star_schema,
)
from qingfeng.validate import run_validation
from qingfeng.viz import render_all
from qingfeng.warehouse import build_warehouse, integrity_check, table_stats

logger = get_logger("pipeline")


def generate_data(cfg: Config, seed: int | None = None) -> None:
    """调用生成器脚本产出模拟数据（独立进程，避免污染当前解释器状态）。"""
    script = cfg.paths.root / "scripts" / "generate_synthetic_data.py"
    if not script.is_file():
        # 自定义配置（如测试用的临时配置）可能不在仓库根目录下，回退到包所在仓库
        script = Path(__file__).resolve().parents[2] / "scripts" / "generate_synthetic_data.py"
    if not script.is_file():
        raise FileNotFoundError(f"找不到数据生成脚本：{script}")
    cmd = [sys.executable, str(script), "--config", str(cfg.source_path)]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    logger.info("运行数据生成器：%s", " ".join(cmd[1:]))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error("数据生成失败：\n%s", result.stderr[-3000:])
        raise RuntimeError("数据生成失败")
    logger.info("数据生成完成（%d 行日志）", len(result.stdout.splitlines()))


def _register_dimension_breakdowns(cfg: Config, star: dict[str, pd.DataFrame], analysis: dict[str, Any]) -> None:
    """把县域 / 项目类别两个维度的汇总表并入分析结果，供报告引用。"""
    analysis["dimension_county"] = dimension_breakdown(cfg, star, "county")
    analysis["dimension_category"] = dimension_breakdown(cfg, star, "category")


def run_pipeline(
    cfg: Config | None = None,
    *,
    regenerate: bool = True,
    seed: int | None = None,
    skip_figures: bool = False,
) -> dict[str, Any]:
    """执行完整管道，返回产物路径与核心结果对象。"""
    cfg = cfg or load_config(ensure_dirs=True)
    cfg.paths.ensure()
    timer = RunTimer()
    logger.info("=" * 72)
    logger.info("%s · 公益项目数据运营分析管道", cfg.org_name)
    logger.info("配置：%s", cfg.source_path)
    logger.info("=" * 72)

    if regenerate:
        with timer.stage("1/10 生成模拟多源数据"):
            generate_data(cfg, seed=seed)

    with timer.stage("2/10 多源数据接入与清单对账"):
        ingest_report = verify_manifest(cfg)
        if len(ingest_report):
            n_fail = int((ingest_report["结果"] == "FAIL").sum())
            if n_fail:
                logger.warning("原始数据与清单存在 %d 处不一致（数据可能在交接后被改动）", n_fail)
            else:
                logger.info("原始数据清单对账通过：%d 项检查全部一致", len(ingest_report))
        source_summary(cfg).to_csv(
            cfg.paths.processed / "ingest_source_summary.csv", index=False, encoding="utf-8-sig"
        )

    with timer.stage("3/10 数据清洗与标准化"):
        cleaned, cleaning_log = run_cleaning(cfg)
        write_cleaned(cfg, cleaned, cleaning_log)
        cleaning_frame = cleaning_log.to_frame()

    with timer.stage("4/10 数据质量校验"):
        validation = run_validation(cfg, cleaned)
        validation_frame = validation.to_frame()
        if not validation.ok:
            logger.warning(
                "存在 %d 条 ERROR 级校验失败，管道继续运行但报告会标注该风险",
                len(validation.failed_errors),
            )

    with timer.stage("5/10 构建星型模型"):
        star = build_star_schema(cfg, cleaned)
        write_star_schema(cfg, star)
        star_schema_summary(star).to_csv(
            cfg.paths.processed / "star_schema_summary.csv", index=False, encoding="utf-8-sig"
        )

    with timer.stage("6/10 装入 SQLite 数据仓库"):
        build_warehouse(cfg, star)
        issues = integrity_check(cfg)
        if issues:
            logger.warning("外键体检发现问题：%s", issues)
        else:
            logger.info("外键体检通过：事实表全部维度键均可匹配")
        stats = table_stats(cfg)
        stats.to_csv(cfg.paths.processed / "warehouse_table_stats.csv", index=False, encoding="utf-8-sig")

    with timer.stage("7/10 计算核心指标体系"):
        kpis = compute_kpis(cfg, star, cleaned)
        panel = kpi_timeseries(cfg, star)
        kpis.to_csv(cfg.paths.processed / "kpi_summary.csv", index=False, encoding="utf-8-sig")
        panel.to_csv(cfg.paths.processed / "monthly_kpi_panel.csv", index=False, encoding="utf-8-sig")

    with timer.stage("8/10 描述性统计 / 交叉 / 趋势 / 队列分析"):
        analysis = run_analysis(cfg, star)
        _register_dimension_breakdowns(cfg, star, analysis)
        write_analysis(cfg, analysis)

    with timer.stage("9/10 异常检测与归因"):
        anomalies = run_anomaly_detection(cfg, star, panel)
        write_anomaly_results(cfg, anomalies)

    figures: dict[str, Path] = {}
    if skip_figures:
        logger.info("10/11 已跳过图表生成（--skip-figures）")
    else:
        with timer.stage("10/11 生成可视化图表"):
            figures = render_all(cfg, star, kpis, analysis, anomalies)

    with timer.stage("11/11 生成分析报告与数据字典"):
        reports = write_reports(
            cfg, kpis, analysis, anomalies, validation_frame, cleaning_frame, star, figures
        )
        reports["data_dictionary"] = _build_data_dictionary(cfg)

    print(timer.summary())

    summary = {
        "config": cfg,
        "cleaning_log": cleaning_frame,
        "validation": validation_frame,
        "star": star,
        "kpis": kpis,
        "monthly_panel": panel,
        "analysis": analysis,
        "anomalies": anomalies,
        "figures": figures,
        "reports": reports,
        "timer": timer,
    }
    _write_run_manifest(cfg, summary)
    _log_final_summary(cfg, summary)
    return summary


def _build_data_dictionary(cfg: Config) -> Path:
    """调用 scripts/build_docs.py 从真实产物导出数据字典。"""
    script = cfg.paths.root / "scripts" / "build_docs.py"
    result = subprocess.run(
        [sys.executable, str(script), "--config", str(cfg.source_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        logger.warning("数据字典生成失败：%s", result.stderr[-800:])
        return cfg.paths.root / "docs" / "data_dictionary.md"
    return cfg.paths.root / "docs" / "data_dictionary.md"


def _write_run_manifest(cfg: Config, summary: dict[str, Any]) -> None:
    """写出本次运行的元信息，便于复现与审计。"""
    kpis = summary["kpis"]
    anomalies = summary["anomalies"]
    manifest = {
        "org": cfg.org_name,
        "seed": cfg.simulation.seed,
        "window": [cfg.simulation.start_date, cfg.simulation.as_of_date],
        "config_file": str(cfg.source_path.relative_to(cfg.paths.root)),
        "star_tables": {name: int(len(df)) for name, df in summary["star"].items()},
        "kpi_count": int(len(kpis)),
        "kpi_alerts": kpis.loc[kpis["状态"] == "预警", "指标名称"].tolist(),
        "kpi_watch": kpis.loc[kpis["状态"] == "关注", "指标名称"].tolist(),
        "anomalies_detected": {
            "monthly": int(len(anomalies["monthly_anomalies"])),
            "county_month": int(len(anomalies["county_month_anomalies"])),
        },
        "cleaning_actions": int(len(summary["cleaning_log"])),
        "validation_rules": int(len(summary["validation"])),
        "validation_failures": int((summary["validation"]["status"] == "FAIL").sum()),
        "reports": {k: str(v.relative_to(cfg.paths.root)) for k, v in summary["reports"].items()},
        "stage_seconds": {name: round(sec, 3) for name, sec, _ in summary["timer"].records},
    }
    path = cfg.paths.reports / "run_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _log_final_summary(cfg: Config, summary: dict[str, Any]) -> None:
    kpis = summary["kpis"]
    logger.info("=" * 72)
    logger.info("管道执行完成")
    logger.info("  数据窗口        : %s ~ %s", cfg.simulation.start_date, cfg.simulation.as_of_date)
    logger.info("  清洗动作        : %d 项", len(summary["cleaning_log"]))
    logger.info("  校验规则        : %d 条（失败 %d 条）",
                len(summary["validation"]), int((summary["validation"]["status"] == "FAIL").sum()))
    logger.info("  星型模型        : %d 张表 / %s 行",
                len(summary["star"]), f"{sum(len(d) for d in summary['star'].values()):,}")
    logger.info("  核心指标        : %d 个（预警 %d / 关注 %d）",
                len(kpis), int((kpis["状态"] == "预警").sum()), int((kpis["状态"] == "关注").sum()))
    logger.info("  检出异常        : 整体 %d / 县域 %d",
                len(summary["anomalies"]["monthly_anomalies"]),
                len(summary["anomalies"]["county_month_anomalies"]))
    logger.info("  图表            : %d 张", len(summary["figures"]))
    for name, path in summary["reports"].items():
        logger.info("  产出            : %-18s %s", name, path.relative_to(cfg.paths.root))
    logger.info("=" * 72)


def verify_pipeline(cfg: Config | None = None) -> dict[str, Any]:
    """只读体检：报告当前仓库中已生成的产物是否齐备、是否自洽。"""
    cfg = cfg or load_config()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"检查项": name, "结果": "PASS" if ok else "FAIL", "说明": detail})

    required_raw = cfg.paths.raw / "_MANIFEST.json"
    add("原始数据清单", required_raw.is_file(),
        str(required_raw.relative_to(cfg.paths.root)) if required_raw.is_file() else "缺失，请先运行 make data")

    try:
        star = load_star_schema(cfg)
        add("星型模型", True, f"{len(star)} 张表")
    except FileNotFoundError as exc:
        star = {}
        add("星型模型", False, str(exc))

    add("SQLite 仓库", cfg.paths.warehouse.is_file(),
        str(cfg.paths.warehouse.relative_to(cfg.paths.root)))

    if cfg.paths.warehouse.is_file():
        issues = integrity_check(cfg)
        add("外键体检", not issues, "全部匹配" if not issues else "; ".join(issues))

    for name, rel in (
        ("主报告", "reports/数据分析报告.md"),
        ("一页纸摘要", "reports/executive_summary.md"),
        ("HTML 仪表盘", "reports/dashboard.html"),
        ("指标字典", "reports/kpi_catalog.csv"),
        ("清洗日志", "data/processed/cleaning_log.csv"),
        ("校验报告", "data/processed/validation_report.csv"),
    ):
        p = cfg.paths.root / rel
        add(name, p.is_file(), rel)

    n_fig = len(list(cfg.paths.figures.glob("*.png")))
    add("图表", n_fig >= 10, f"{n_fig} 张")

    df = pd.DataFrame(checks)
    ok = bool((df["结果"] == "PASS").all()) if len(df) else False
    logger.info("体检结果：%d/%d 项通过", int((df["结果"] == "PASS").sum()), len(df))
    logger.info("\n%s", df.to_string(index=False))
    return {"checks": df, "ok": ok}


def load_artifacts(cfg: Config | None = None) -> dict[str, Any]:
    """读取已产出的中间结果（供 notebook 复用，避免重复计算）。"""
    cfg = cfg or load_config()
    out: dict[str, Any] = {"config": cfg}
    out["cleaned"] = load_cleaned(cfg)
    out["star"] = load_star_schema(cfg)
    panel_path = cfg.paths.processed_file("monthly_kpi_panel.csv")
    if panel_path.is_file():
        panel = pd.read_csv(panel_path, encoding="utf-8-sig")
        panel["month_dt"] = pd.to_datetime(panel["year_month"] + "-01")
        out["monthly_panel"] = panel
    kpi_path = cfg.paths.processed_file("kpi_summary.csv")
    if kpi_path.is_file():
        out["kpis"] = pd.read_csv(kpi_path, encoding="utf-8-sig")
    for name in ("anomaly_monthly", "anomaly_county_month", "anomaly_attribution", "anomaly_evaluation",
                 "descriptive_statistics", "cross_analysis_tests", "trend_summary",
                 "monthly_seasonality", "project_scorecard", "correlation_matrix"):
        p = cfg.paths.processed_file(f"{name}.csv")
        if p.is_file():
            out[name] = pd.read_csv(p, encoding="utf-8-sig")
    return out


def load_cleaning_log(cfg: Config) -> pd.DataFrame:
    p = cfg.paths.processed_file("cleaning_log.csv")
    return pd.read_csv(p, encoding="utf-8-sig") if p.is_file() else pd.DataFrame()


__all__ = [
    "run_pipeline", "verify_pipeline", "load_artifacts", "load_cleaning_log",
    "generate_data", "CleaningLog",
]
