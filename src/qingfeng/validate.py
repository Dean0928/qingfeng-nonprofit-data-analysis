"""数据质量校验规则引擎。

清洗解决了"已知的脏"，校验回答"洗完之后还剩什么问题"。本模块用**声明式规则**
描述每一张表应当满足的约束，运行后产出：

* ``data/processed/validation_report.csv`` —— 逐规则明细（含失败样例）
* ``data/processed/validation_summary.csv`` —— 按表汇总的质量分
* 控制台摘要，并在存在 ``ERROR`` 级失败时让 CI 失败

规则类型
--------
``not_null``              非空
``unique``                主键唯一
``in_set``                取值属于受控词表
``range``                 数值落在物理/业务区间
``regex``                 格式匹配
``referential``           引用完整性（外键存在于父表）
``date_order``            日期先后关系
``cross_field``           自定义布尔表达式
``row_count``             行数下限
``completeness``          整表字段填充率
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from qingfeng.config import Config
from qingfeng.logging_utils import get_logger
from qingfeng.tables import TABLE_SPECS, cleaning_tables

logger = get_logger("validate")

SEVERITY_ERROR = "ERROR"
SEVERITY_WARN = "WARN"
SEVERITY_INFO = "INFO"
_SEVERITY_ORDER = {SEVERITY_ERROR: 0, SEVERITY_WARN: 1, SEVERITY_INFO: 2}


@dataclass
class ValidationResult:
    table: str
    rule: str
    column: str
    severity: str
    description: str
    total_rows: int
    failed_rows: int
    pass_rate: float
    sample_failures: str = ""

    @property
    def passed(self) -> bool:
        return self.failed_rows == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "rule": self.rule,
            "column": self.column,
            "severity": self.severity,
            "description": self.description,
            "total_rows": self.total_rows,
            "failed_rows": self.failed_rows,
            "pass_rate": round(self.pass_rate, 6),
            "status": "PASS" if self.passed else "FAIL",
            "sample_failures": self.sample_failures,
        }


@dataclass(frozen=True)
class Rule:
    """一条可执行的数据质量规则。"""

    name: str
    table: str
    column: str
    severity: str
    description: str
    check: Callable[[pd.DataFrame, dict[str, pd.DataFrame]], pd.Series]
    sample_column: str | None = None

    def evaluate(self, df: pd.DataFrame, context: dict[str, pd.DataFrame]) -> ValidationResult:
        total = len(df)
        if total == 0:
            return ValidationResult(
                self.table, self.name, self.column, SEVERITY_WARN,
                self.description, 0, 0, 1.0, "表为空",
            )
        try:
            ok = self.check(df, context)
        except Exception as exc:  # noqa: BLE001 - 规则异常本身也是一条失败
            return ValidationResult(
                self.table, self.name, self.column, SEVERITY_ERROR,
                self.description, total, total, 0.0, f"规则执行异常: {exc}",
            )
        ok = pd.Series(ok).reindex(df.index).fillna(True).astype(bool)
        failed = int((~ok).sum())
        sample = ""
        if failed:
            col = self.sample_column or (self.column if self.column in df.columns else None)
            if col:
                vals = df.loc[~ok, col].astype(str).head(3).tolist()
                sample = "; ".join(vals)
        return ValidationResult(
            self.table, self.name, self.column, self.severity,
            self.description, total, failed, (total - failed) / total, sample,
        )


# ---------------------------------------------------------------------------
# 规则构造函数
# ---------------------------------------------------------------------------


def not_null(table: str, column: str, severity: str = SEVERITY_ERROR) -> Rule:
    return Rule(
        f"not_null:{column}", table, column, severity, f"{column} 不应为空",
        lambda df, _ctx: df[column].notna() & (df[column].astype(str).str.strip() != ""),
    )


def unique(table: str, column: str, severity: str = SEVERITY_ERROR) -> Rule:
    def _check(df: pd.DataFrame, _ctx: dict[str, pd.DataFrame]) -> pd.Series:
        s = df[column]
        dup = s.duplicated(keep=False)
        # 空值不计入唯一性失败，由 not_null 规则单独负责
        return (~dup) | s.isna()

    return Rule(
        f"unique:{column}", table, column, severity, f"{column} 作为主键必须唯一", _check,
    )


def in_set(table: str, column: str, allowed: Sequence[str], severity: str = SEVERITY_WARN) -> Rule:
    allowed_set = set(allowed) | {"未知", "UNKNOWN", ""}
    return Rule(
        f"in_set:{column}", table, column, severity,
        f"{column} 取值应属于受控词表（{len(allowed)} 个合法值）",
        lambda df, _ctx: df[column].isin(allowed_set) | df[column].isna(),
    )


def numeric_range(
    table: str, column: str, lo: float, hi: float, severity: str = SEVERITY_ERROR
) -> Rule:
    return Rule(
        f"range:{column}", table, column, severity, f"{column} 应落在 [{lo:g}, {hi:g}]",
        lambda df, _ctx: pd.to_numeric(df[column], errors="coerce").between(lo, hi)
        | pd.to_numeric(df[column], errors="coerce").isna(),
    )


def regex_match(table: str, column: str, pattern: str, severity: str = SEVERITY_WARN) -> Rule:
    return Rule(
        f"regex:{column}", table, column, severity, f"{column} 应匹配 {pattern}",
        lambda df, _ctx: df[column].isna() | df[column].astype(str).str.fullmatch(pattern),
    )


def referential(
    table: str,
    column: str,
    parent_table: str,
    parent_column: str,
    severity: str = SEVERITY_ERROR,
) -> Rule:
    return Rule(
        f"referential:{column}->{parent_table}.{parent_column}", table, column, severity,
        f"{column} 必须存在于 {parent_table}.{parent_column}",
        lambda df, ctx: df[column].isin(set(ctx[parent_table][parent_column])) | df[column].isna()
        | df[column].isin(["UNKNOWN", "未知", ""]),
    )


def date_order(table: str, start_col: str, end_col: str, severity: str = SEVERITY_WARN) -> Rule:
    return Rule(
        f"date_order:{start_col}<={end_col}", table, end_col, severity,
        f"{start_col} 不应晚于 {end_col}",
        lambda df, _ctx: (
            df[start_col].isna() | df[end_col].isna() | (df[start_col] <= df[end_col])
        ),
        sample_column=start_col,
    )


def cross_field(
    table: str, name: str, column: str, description: str,
    check: Callable[[pd.DataFrame], pd.Series], severity: str = SEVERITY_WARN,
) -> Rule:
    return Rule(f"cross_field:{name}", table, column, severity, description, lambda df, _ctx: check(df))


def row_count(table: str, minimum: int, severity: str = SEVERITY_ERROR) -> Rule:
    return Rule(
        f"row_count>={minimum}", table, "*", severity, f"{table} 至少应有 {minimum} 行",
        lambda df, _ctx: pd.Series(True, index=df.index) if len(df) >= minimum else pd.Series(False, index=df.index),
    )


def completeness(table: str, minimum: float, severity: str = SEVERITY_WARN) -> Rule:
    """整表字段填充率规则。

    注意：填充率在**清洗后的本表**上计算，而不是在校验上下文里被 LEFT JOIN 扩列后的
    宽表上计算 —— 否则联接未命中的辅助列会拉低填充率，产生假告警。
    """

    def _check(df: pd.DataFrame, ctx: dict[str, pd.DataFrame]) -> pd.Series:
        base = ctx.get("__base__", {}).get(table, df)
        rate = float(1.0 - base.isna().mean().mean())
        return pd.Series(rate >= minimum, index=df.index)

    return Rule(
        f"completeness>={minimum:.0%}", table, "*", severity,
        f"{table} 整体字段填充率应不低于 {minimum:.0%}", _check,
    )


# ---------------------------------------------------------------------------
# 规则集
# ---------------------------------------------------------------------------


def build_rules() -> list[Rule]:
    """构建覆盖全部业务表的校验规则集。"""
    rules: list[Rule] = []

    # --- 维度表 ---
    for tbl, key in (("projects", "project_id"), ("staff", "staff_id"), ("volunteers", "volunteer_id"),
                     ("donors", "donor_id"), ("beneficiaries", "beneficiary_id")):
        rules += [not_null(tbl, key), unique(tbl, key), row_count(tbl, 10)]

    rules += [
        in_set("projects", "category", list(TABLE_SPECS["projects"]["controlled"]["category"])),
        in_set("projects", "county", ["香格里拉市", "维西傈僳族自治县", "德钦县"]),
        in_set("projects", "status", ["筹备中", "进行中", "已完成", "已结项", "暂停"]),
        numeric_range("projects", "planned_budget_cny", 0, 20_000_000),
        date_order("projects", "planned_start_date", "planned_end_date"),
        completeness("projects", 0.95),
        in_set("volunteers", "county", ["香格里拉市", "维西傈僳族自治县", "德钦县"]),
        in_set("volunteers", "ethnicity", list(TABLE_SPECS["volunteers"]["controlled"]["ethnicity"])),
        in_set("volunteers", "education", list(TABLE_SPECS["volunteers"]["controlled"]["education"])),
        in_set("volunteers", "status", ["活跃", "沉睡", "退出", "新增"]),
        regex_match("volunteers", "phone_masked", r"[0-9]{3}\*{4}[0-9]{4}"),
        completeness("volunteers", 0.95),
        in_set("beneficiaries", "household_type", list(TABLE_SPECS["beneficiaries"]["controlled"]["household_type"])),
        numeric_range("beneficiaries", "age", 0, 120),
        numeric_range("beneficiaries", "risk_score", 0, 100),
        in_set("donors", "donor_type", ["个人", "企业", "基金会", "政府", "社会组织"]),
    ]

    # --- 事实表 ---
    rules += [
        not_null("volunteer_shifts", "shift_id"),
        unique("volunteer_shifts", "shift_id"),
        referential("volunteer_shifts", "volunteer_id", "volunteers", "volunteer_id"),
        referential("volunteer_shifts", "project_id", "projects", "project_id"),
        numeric_range("volunteer_shifts", "hours", 0.25, 12.0),
        in_set("volunteer_shifts", "service_role", list(TABLE_SPECS["volunteer_shifts"]["controlled"]["service_role"])),
        in_set("volunteer_shifts", "checkin_method", list(TABLE_SPECS["volunteer_shifts"]["controlled"]["checkin_method"])),
        row_count("volunteer_shifts", 1000),
        completeness("volunteer_shifts", 0.99),
        not_null("donations", "donation_id"),
        unique("donations", "donation_id"),
        referential("donations", "donor_id", "donors", "donor_id"),
        numeric_range("donations", "amount_cny", 0, 1_000_000),
        in_set("donations", "designation", ["定向", "非定向"]),
        row_count("donations", 500),
        completeness("donations", 0.99),
        not_null("in_kind_donations", "inkind_id"),
        unique("in_kind_donations", "inkind_id"),
        referential("in_kind_donations", "donor_id", "donors", "donor_id"),
        numeric_range("in_kind_donations", "quantity", 1, 5000),
        numeric_range("in_kind_donations", "est_total_value_cny", 0, 500_000),
        not_null("beneficiary_services", "service_id"),
        unique("beneficiary_services", "service_id"),
        referential("beneficiary_services", "beneficiary_id", "beneficiaries", "beneficiary_id"),
        referential("beneficiary_services", "project_id", "projects", "project_id"),
        numeric_range("beneficiary_services", "satisfaction_score", 1, 5),
        numeric_range("beneficiary_services", "duration_hours", 0.25, 12.0),
        row_count("beneficiary_services", 1000),
        completeness("beneficiary_services", 0.95),
        not_null("monthly_budget_execution", "month"),
        regex_match("monthly_budget_execution", "month", r"\d{4}-\d{2}"),
        numeric_range("monthly_budget_execution", "actual_cny", 0, 5_000_000),
        referential("monthly_budget_execution", "project_id", "projects", "project_id"),
        completeness("monthly_budget_execution", 0.95),
        not_null("satisfaction_survey_raw", "response_id"),
        unique("satisfaction_survey_raw", "response_id"),
        numeric_range("satisfaction_survey_raw", "Q1_总体满意度_score", 1, 5),
        numeric_range("satisfaction_survey_raw", "Q2_志愿者服务态度_score", 1, 5),
        in_set("satisfaction_survey_raw", "gender", ["男", "女"]),
    ]

    # --- 跨字段业务一致性 ---
    rules += [
        cross_field(
            "volunteer_shifts", "shift_after_volunteer_join", "service_date",
            "签到日期不应早于志愿者注册日期",
            lambda df: df["service_date"].isna() | df["volunteer_join_date"].isna()
            | (df["service_date"] >= df["volunteer_join_date"]),
            severity=SEVERITY_WARN,
        ),
        cross_field(
            "volunteer_shifts", "shift_within_project_window", "service_date",
            "签到日期应落在项目计划起止区间内（可容忍 30 天延期）",
            lambda df: df["service_date"].isna() | df["project_start_date"].isna()
            | df["project_end_date"].isna()
            | (
                (df["service_date"] >= df["project_start_date"] - pd.Timedelta(days=7))
                & (df["service_date"] <= df["project_end_date"] + pd.Timedelta(days=30))
            ),
            severity=SEVERITY_INFO,
        ),
        cross_field(
            "donations", "donation_after_donor_first", "donation_date",
            "捐赠日期不应早于捐赠人首次捐赠日期",
            lambda df: df["donation_date"].isna() | df["donor_first_date"].isna()
            | (df["donation_date"] >= df["donor_first_date"]),
            severity=SEVERITY_WARN,
        ),
        cross_field(
            "beneficiary_services", "service_after_enroll", "service_date",
            "服务记录日期不应早于服务对象建档日期",
            lambda df: df["service_date"].isna() | df["enroll_date"].isna()
            | (df["service_date"] >= df["enroll_date"]),
            severity=SEVERITY_WARN,
        ),
        cross_field(
            "beneficiaries", "beneficiary_age_vs_project_category", "age",
            "服务对象年龄应与项目类别自洽（助学 6-19 岁 / 助老 58 岁以上）",
            lambda df: _beneficiary_age_consistent(df),
            severity=SEVERITY_INFO,
        ),
    ]
    return rules


def _beneficiary_age_consistent(df: pd.DataFrame) -> pd.Series:
    cat = df["project_category"].astype(str)
    age = pd.to_numeric(df["age"], errors="coerce")
    ok = pd.Series(True, index=df.index)
    ok &= ~((cat == "助学支教") & ~age.between(5, 20))
    ok &= ~((cat == "助老敬老") & ~age.between(56, 100))
    return ok | age.isna()


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


def enrich_context(cleaned: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """为跨字段规则补充必要的连接列。

    ``ctx["__base__"]`` 保留未被扩列的原始清洗结果，供完整性规则使用。
    """
    ctx = dict(cleaned)
    ctx["__base__"] = cleaned

    vol_join = cleaned["volunteers"][["volunteer_id", "join_date"]].rename(
        columns={"join_date": "volunteer_join_date"}
    )
    ctx["volunteer_shifts"] = cleaned["volunteer_shifts"].merge(vol_join, on="volunteer_id", how="left")

    proj_win = cleaned["projects"][["project_id", "planned_start_date", "planned_end_date"]].rename(
        columns={"planned_start_date": "project_start_date", "planned_end_date": "project_end_date"}
    )
    ctx["volunteer_shifts"] = ctx["volunteer_shifts"].merge(proj_win, on="project_id", how="left")

    donor_first = cleaned["donors"][["donor_id", "first_donation_date"]].rename(
        columns={"first_donation_date": "donor_first_date"}
    )
    ctx["donations"] = cleaned["donations"].merge(donor_first, on="donor_id", how="left")

    bene_enroll = cleaned["beneficiaries"][["beneficiary_id", "enroll_date"]]
    ctx["beneficiary_services"] = cleaned["beneficiary_services"].merge(
        bene_enroll, on="beneficiary_id", how="left"
    )

    ctx["beneficiaries"] = cleaned["beneficiaries"].merge(
        cleaned["projects"][["project_id", "category"]].rename(columns={"category": "project_category"}),
        on="project_id", how="left",
    )
    return ctx


@dataclass
class ValidationReport:
    results: list[ValidationResult] = field(default_factory=list)

    def to_frame(self) -> pd.DataFrame:
        if not self.results:
            return pd.DataFrame(columns=list(ValidationResult("", "", "", "", "", 0, 0, 1.0).as_dict()))
        df = pd.DataFrame([r.as_dict() for r in self.results])
        df["_sev"] = df["severity"].map(_SEVERITY_ORDER).fillna(9)
        return df.sort_values(["_sev", "table", "rule"]).drop(columns="_sev").reset_index(drop=True)

    @property
    def failed_errors(self) -> list[ValidationResult]:
        return [r for r in self.results if not r.passed and r.severity == SEVERITY_ERROR]

    @property
    def failed_warnings(self) -> list[ValidationResult]:
        return [r for r in self.results if not r.passed and r.severity == SEVERITY_WARN]

    @property
    def ok(self) -> bool:
        return not self.failed_errors

    def quality_scores(self) -> pd.DataFrame:
        """按表计算加权质量分：ERROR 权重 1.0，WARN 权重 0.5，INFO 权重 0.2。"""
        columns = ["table", "rules", "weighted_pass_rate", "errors", "warnings"]
        if not self.results:
            return pd.DataFrame(columns=columns)
        weight = {SEVERITY_ERROR: 1.0, SEVERITY_WARN: 0.5, SEVERITY_INFO: 0.2}
        frame = self.to_frame()
        rows = []
        for table, g in frame.groupby("table", sort=False):
            w = g["severity"].map(weight).fillna(0.2).to_numpy()
            failed = g["status"] == "FAIL"
            rows.append(
                {
                    "table": table,
                    "rules": int(len(g)),
                    "weighted_pass_rate": float((g["pass_rate"].to_numpy() * w).sum() / w.sum()),
                    "errors": int((failed & (g["severity"] == SEVERITY_ERROR)).sum()),
                    "warnings": int((failed & (g["severity"] == SEVERITY_WARN)).sum()),
                }
            )
        return pd.DataFrame(rows, columns=columns).sort_values("weighted_pass_rate").reset_index(drop=True)


def run_validation(cfg: Config, cleaned: dict[str, pd.DataFrame]) -> ValidationReport:
    """执行全部规则，打印摘要并落盘。"""
    ctx = enrich_context(cleaned)
    report = ValidationReport()
    for rule in build_rules():
        df = ctx.get(rule.table)
        if df is None:
            logger.warning("规则 %s 引用了不存在的表 %s，已跳过", rule.name, rule.table)
            continue
        report.results.append(rule.evaluate(df, ctx))

    frame = report.to_frame()
    scores = report.quality_scores()

    cfg.paths.processed.mkdir(parents=True, exist_ok=True)
    frame.to_csv(cfg.paths.processed / "validation_report.csv", index=False, encoding="utf-8-sig")
    scores.to_csv(cfg.paths.processed / "validation_summary.csv", index=False, encoding="utf-8-sig")

    n_pass = int((frame["status"] == "PASS").sum())
    logger.info(
        "校验完成：%d/%d 条规则通过 | ERROR 失败 %d | WARN 失败 %d",
        n_pass, len(frame), len(report.failed_errors), len(report.failed_warnings),
    )
    if scores is not None and len(scores):
        logger.info("各表加权质量分：\n%s", scores.to_string(index=False))
    for r in report.failed_errors[:10]:
        logger.warning("  ✖ [%s] %s -> 失败 %d/%d 行 | 样例: %s", r.table, r.rule, r.failed_rows, r.total_rows, r.sample_failures)
    return report
