"""数据清洗与标准化。

本模块把"能拿到手的多源脏数据"变成"可以进入分析的事实表"，是整条管道里
最能体现工程能力的一层。设计原则：

1. **每个清洗动作都留痕**：所有函数返回 ``(cleaned, actions)``，动作记录汇入
   :class:`CleaningLog`，最终落盘为 ``data/processed/cleaning_log.csv``。分析报告中
   会引用这张表回答"数据质量到底提升了多少"。
2. **可被 ground truth 检验**：生成器在 ``data/raw/_ground_truth/injected_defects.csv``
   记录了每一个注入缺陷的位置，因此本模块的精确率/召回率可以被定量评估
   （见 ``tests/test_clean.py`` 与 ``reports/``）。
3. **不做静默修正**：任何"猜测性"修复（如按物理上限截断、把悬空外键归入 UNKNOWN）
   都会写入日志并标记 ``is_corrected`` 质量标签，便于下游区分原始值与修正值。

七类缺陷与本模块的对应处理：

============================  ==================================================
缺陷类型                       处理策略
============================  ==================================================
duplicate_row                 按主键 / 业务组合键去重
missing_value                 关键维度回填 "未知"，度量缺失则标记并排除
malformed_date                多格式日期解析（6 种格式）
unit_inconsistency            单位正则解析并统一到基准单位
whitespace_or_widechar        去空格、全角转半角、统一分隔符
orphan_foreign_key            无法匹配的维度键归入 UNKNOWN 成员并计数
extreme_outlier               按业务物理边界截断 / 按统计规则置缺
============================  ==================================================
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from qingfeng.config import Config
from qingfeng.logging_utils import get_logger

logger = get_logger("clean")

UNKNOWN = "UNKNOWN"  # 全仓库统一的未知成员标签，与维度表 -1 代理键对应

# ---------------------------------------------------------------------------
# 日期与单位解析规则
# ---------------------------------------------------------------------------

DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日",
    "%Y.%m.%d",
    "%Y%m%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
)

# 单位 -> 相对基准单位的换算系数（时长：小时；数量：原单位不变）
UNIT_FACTORS_HOURS: dict[str, float] = {
    "": 1.0, "h": 1.0, "hr": 1.0, "hrs": 1.0, "hour": 1.0, "hours": 1.0,
    "小时": 1.0, "个小时": 1.0, "時": 1.0,
    "min": 1 / 60, "mins": 1 / 60, "minute": 1 / 60, "minutes": 1 / 60,
    "分钟": 1 / 60, "分": 1 / 60,
    "s": 1 / 3600, "sec": 1 / 3600, "秒": 1 / 3600,
}

_QUANTITY_RE = re.compile(r"^\s*(?P<num>[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?P<unit>[^\d\s]*)\s*$")
_PUNCT_MAP = {"·": "-", "—": "-", "–": "-", "－": "-", "　": " "}


# ---------------------------------------------------------------------------
# 清洗日志
# ---------------------------------------------------------------------------


@dataclass
class CleaningAction:
    table: str
    column: str
    issue: str
    action: str
    affected_rows: int
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "issue": self.issue,
            "action": self.action,
            "affected_rows": self.affected_rows,
            "note": self.note,
        }


@dataclass
class CleaningLog:
    actions: list[CleaningAction] = field(default_factory=list)

    def add(self, table: str, column: str, issue: str, action: str, rows: int, note: str = "") -> None:
        if rows <= 0:
            return
        self.actions.append(CleaningAction(table, column, issue, action, int(rows), note))
        logger.info("  [%s] %s.%s -> %s（%d 行）", table, issue, column, action, rows)

    def extend(self, other: Iterable[CleaningAction]) -> None:
        self.actions.extend(other)

    def to_frame(self) -> pd.DataFrame:
        if not self.actions:
            return pd.DataFrame(
                columns=["table", "column", "issue", "action", "affected_rows", "note"]
            )
        df = pd.DataFrame([a.as_dict() for a in self.actions])
        return df.sort_values(["table", "issue", "column"]).reset_index(drop=True)

    def summary(self) -> pd.DataFrame:
        """按 (表, 问题类型) 汇总影响行数，便于画"清洗漏斗"。"""
        df = self.to_frame()
        if df.empty:
            return df
        return (
            df.groupby(["table", "issue"], as_index=False)
            .agg(affected_rows=("affected_rows", "sum"), actions=("action", "count"))
            .sort_values("affected_rows", ascending=False)
            .reset_index(drop=True)
        )


# ---------------------------------------------------------------------------
# 1. 文本标准化
# ---------------------------------------------------------------------------


def normalize_text(value: Any) -> Any:
    """去除首尾空白、全角转半角、统一连字符与全角空格。"""
    if not isinstance(value, str):
        return value
    s = unicodedata.normalize("NFKC", value)
    for src, dst in _PUNCT_MAP.items():
        s = s.replace(src, dst)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_text_columns(
    df: pd.DataFrame, columns: Sequence[str], table: str, log: CleaningLog
) -> pd.DataFrame:
    df = df.copy()
    for col in columns:
        if col not in df.columns:
            continue
        original = df[col]
        cleaned = original.map(normalize_text)
        changed = int(((original.astype(str) != cleaned.astype(str)) & original.notna()).sum())
        if changed:
            log.add(table, col, "whitespace_or_widechar", "去除空白/全角转半角", changed)
        df[col] = cleaned
    return df


# ---------------------------------------------------------------------------
# 2. 日期解析
# ---------------------------------------------------------------------------


def parse_mixed_dates(
    series: pd.Series, table: str, column: str, log: CleaningLog
) -> tuple[pd.Series, pd.Series]:
    """把多格式混用的日期列统一为 ``datetime64[ns]``。

    Returns
    -------
    (parsed, matched_format)
        ``matched_format`` 记录每行命中的格式名，未命中为 ``"unparsed"``。
    """
    result = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    matched = pd.Series("unparsed", index=series.index, dtype=object)
    text = series.astype(str).map(normalize_text)

    for fmt in DATE_FORMATS:
        remaining = result.isna() & text.notna() & (text != "") & (text.str.lower() != "nan") & (text != "NaT")
        if not remaining.any():
            break
        parsed = pd.to_datetime(text[remaining], format=fmt, errors="coerce")
        hit = parsed.notna()
        if hit.any():
            idx = parsed.index[hit]
            result.loc[idx] = parsed.loc[idx]
            matched.loc[idx] = fmt
    # 兜底：交给 pandas 自行推断
    remaining = result.isna() & text.notna() & (text != "") & (text.str.lower() != "nan") & (text != "NaT")
    if remaining.any():
        parsed = pd.to_datetime(text[remaining], errors="coerce", dayfirst=False)
        hit = parsed.notna()
        if hit.any():
            idx = parsed.index[hit]
            result.loc[idx] = parsed.loc[idx]
            matched.loc[idx] = "inferred"

    n_fmt = int((matched != "unparsed").sum())
    n_unparsed = int(matched.eq("unparsed").sum() - series.isna().sum())
    if len(DATE_FORMATS) > 1:
        log.add(table, column, "malformed_date", "统一为 ISO 日期（多格式解析）", n_fmt)
    if n_unparsed > 0:
        log.add(table, column, "date_unparseable", "置空并标记", n_unparsed)
    return result, matched


# ---------------------------------------------------------------------------
# 3. 数值 + 单位解析
# ---------------------------------------------------------------------------


def parse_quantity_with_unit(
    series: pd.Series,
    table: str,
    column: str,
    log: CleaningLog,
    *,
    kind: str = "hours",
) -> tuple[pd.Series, pd.Series]:
    """解析 ``"3.5"`` / ``"3.5h"`` / ``"210min"`` / ``"5件"`` 这类混合值。

    ``kind="hours"`` 时按 :data:`UNIT_FACTORS_HOURS` 换算到小时；
    其它情况仅剥离单位文本，保留数值（并记录单位歧义）。
    """
    factors = UNIT_FACTORS_HOURS if kind == "hours" else {}
    values = pd.Series(np.nan, index=series.index, dtype="float64")
    units = pd.Series("", index=series.index, dtype=object)

    text = series.astype(str).map(normalize_text)
    for idx, raw in text.items():
        if raw in {"", "nan", "NaT", "None"}:
            continue
        m = _QUANTITY_RE.match(raw)
        if not m:
            continue
        num = float(m.group("num"))
        unit = m.group("unit").lower().replace(" ", "")
        units.at[idx] = unit
        if kind == "hours":
            factor = factors.get(unit)
            if factor is None:
                # 未知单位：记录并保留原值，避免过度推断
                units.at[idx] = f"?{unit}"
                factor = 1.0
            values.at[idx] = num * factor
        else:
            values.at[idx] = num

    # 纯数值列（解析失败但本身是数字）
    numeric_direct = pd.to_numeric(series, errors="coerce")
    fill = values.isna() & numeric_direct.notna()
    values.loc[fill] = numeric_direct.loc[fill].astype(float)

    n_with_unit = int((units != "").sum())
    n_unknown_unit = int(units.astype(str).str.startswith("?").sum())
    if n_with_unit:
        log.add(table, column, "unit_inconsistency", f"剥离单位并统一为{kind}", n_with_unit)
    if n_unknown_unit:
        log.add(table, column, "unknown_unit", "保留原数值并标记待确认", n_unknown_unit)
    return values, units


# ---------------------------------------------------------------------------
# 4. 去重
# ---------------------------------------------------------------------------


def drop_duplicates(
    df: pd.DataFrame,
    table: str,
    log: CleaningLog,
    *,
    exact_subset: Sequence[str] | None = None,
    business_keys: Sequence[str] | None = None,
    keep: str = "first",
) -> pd.DataFrame:
    """先按业务主键去重（重复导入场景），再按全字段去重。"""
    before = len(df)
    if business_keys:
        keys = [k for k in business_keys if k in df.columns]
        if keys:
            df = df.drop_duplicates(subset=keys, keep=keep)
            log.add(
                table,
                "+".join(keys),
                "duplicate_row",
                "按业务组合键去重",
                before - len(df),
                note="应对批量重复导入",
            )
    if exact_subset:
        before2 = len(df)
        subset = [c for c in exact_subset if c in df.columns]
        df = df.drop_duplicates(subset=subset, keep=keep)
        log.add(table, "+".join(subset), "duplicate_row", "按全字段去重", before2 - len(df))
    elif not business_keys:
        before2 = len(df)
        df = df.drop_duplicates(keep=keep)
        log.add(table, "*", "duplicate_row", "按全字段去重", before2 - len(df))
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. 缺失值处理
# ---------------------------------------------------------------------------


def fill_dimension_nulls(
    df: pd.DataFrame, columns: Sequence[str], table: str, log: CleaningLog, fill_value: str = UNKNOWN
) -> pd.DataFrame:
    df = df.copy()
    for col in columns:
        if col not in df.columns:
            continue
        n = int(df[col].isna().sum())
        if n:
            df[col] = df[col].fillna(fill_value)
            log.add(table, col, "missing_value", f"维度字段回填“{fill_value}”", n)
    return df


def drop_rows_missing_measures(
    df: pd.DataFrame, columns: Sequence[str], table: str, log: CleaningLog
) -> pd.DataFrame:
    """度量字段缺失时无法参与汇总，直接剔除并记录。"""
    cols = [c for c in columns if c in df.columns]
    if not cols:
        return df
    mask = df[cols].isna().all(axis=1)
    n = int(mask.sum())
    if n:
        log.add(table, "+".join(cols), "missing_value", "剔除关键度量缺失的记录", n)
    return df.loc[~mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 6. 外键完整性
# ---------------------------------------------------------------------------


def resolve_orphan_keys(
    df: pd.DataFrame,
    column: str,
    valid_values: Iterable[Any],
    table: str,
    log: CleaningLog,
    *,
    strategy: str = "unknown",
) -> tuple[pd.DataFrame, pd.Series]:
    """处理悬空外键。

    ``strategy="unknown"`` 归入 ``"UNKNOWN"`` 成员（保留记录，不丢业务量）；
    ``strategy="drop"`` 直接删除。返回 ``(df, is_orphan_flags)``。
    """
    df = df.copy()
    valid = set(valid_values)
    present = df[column].notna()
    is_orphan = present & ~df[column].isin(valid)
    n = int(is_orphan.sum())
    if n:
        if strategy == "drop":
            df = df.loc[~is_orphan].reset_index(drop=True)
            log.add(table, column, "orphan_foreign_key", "剔除无法匹配维度的记录", n)
            is_orphan = pd.Series(False, index=df.index)
        else:
            df.loc[is_orphan, column] = "UNKNOWN"
            log.add(table, column, "orphan_foreign_key", "归入 UNKNOWN 维度成员", n)
    return df, is_orphan


# ---------------------------------------------------------------------------
# 7. 异常值处理
# ---------------------------------------------------------------------------


def clip_to_physical_range(
    df: pd.DataFrame,
    column: str,
    lo: float,
    hi: float,
    table: str,
    log: CleaningLog,
) -> tuple[pd.DataFrame, pd.Series]:
    """按业务物理边界截断，并返回被截断标记。

    例：单次志愿服务时长不可能为负、也不应超过 12 小时；捐赠金额不可能为负。
    """
    df = df.copy()
    s = pd.to_numeric(df[column], errors="coerce")
    flags = s.notna() & ((s < lo) | (s > hi))
    n = int(flags.sum())
    if n:
        df[column] = s.clip(lower=lo, upper=hi)
        log.add(
            table, column, "extreme_outlier",
            f"按业务边界截断到 [{lo:g}, {hi:g}]", n,
            note="截断而非删除，以保留该次服务/捐赠的业务存在性",
        )
    else:
        df[column] = s
    return df, flags


def flag_statistical_outliers(
    df: pd.DataFrame, column: str, table: str, log: CleaningLog, *, iqr_multiplier: float = 1.5
) -> tuple[pd.DataFrame, pd.Series]:
    """用 Tukey 箱线规则标记统计离群点（**只标记不删除**）。"""
    s = pd.to_numeric(df[column], errors="coerce")
    q1, q3 = s.quantile([0.25, 0.75])
    iqr = q3 - q1
    lo, hi = q1 - iqr_multiplier * iqr, q3 + iqr_multiplier * iqr
    flags = s.notna() & ((s < lo) | (s > hi))
    log.add(table, column, "statistical_outlier", "标记为离群点（保留原值）", int(flags.sum()))
    return df, flags


# ---------------------------------------------------------------------------
# 8. 单表清洗入口
# ---------------------------------------------------------------------------


def clean_projects(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    df = normalize_text_columns(df, ["project_name", "county", "township", "status", "funding_source"], "projects", log)
    for col in ("planned_start_date", "planned_end_date", "actual_end_date"):
        df = df.copy()
        df[col], _ = parse_mixed_dates(df[col], "projects", col, log)
    # 计划结束日期早于开始日期 -> 交换（业务上必然写反）
    bad = df["planned_end_date"].notna() & (df["planned_end_date"] < df["planned_start_date"])
    if bad.any():
        log.add("projects", "planned_end_date", "invalid_date_order", "交换起止日期", int(bad.sum()))
        df.loc[bad, ["planned_start_date", "planned_end_date"]] = df.loc[
            bad, ["planned_end_date", "planned_start_date"]
        ].to_numpy()
    n_neg = int((df["planned_budget_cny"] < 0).sum())
    if n_neg:
        df["planned_budget_cny"] = df["planned_budget_cny"].abs()
        log.add("projects", "planned_budget_cny", "extreme_outlier", "负数预算取绝对值并复核", n_neg)
    df = df.drop(columns=["seasonality"], errors="ignore")
    return drop_duplicates(df, "projects", log, exact_subset=["project_id"])


def clean_staff(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    df = normalize_text_columns(df, ["staff_name", "role", "office_county"], "staff", log)
    df["hire_date"], _ = parse_mixed_dates(df["hire_date"], "staff", "hire_date", log)
    return drop_duplicates(df, "staff", log, exact_subset=["staff_id"])


def clean_volunteers(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    df = normalize_text_columns(
        df, ["volunteer_name", "county", "township", "occupation", "education", "status", "skills"],
        "volunteers", log,
    )
    df["join_date"], _ = parse_mixed_dates(df["join_date"], "volunteers", "join_date", log)
    df = fill_dimension_nulls(df, ["county", "township", "occupation", "education"], "volunteers", log)
    # 出生年份物理边界：1900 ~ 当年
    df, _ = clip_to_physical_range(df, "birth_year", 1930, 2015, "volunteers", log)
    df["birth_year"] = df["birth_year"].round().astype("Int64")
    df = drop_duplicates(df, "volunteers", log, exact_subset=["volunteer_id"])
    return df


def clean_shifts(df: pd.DataFrame, log: CleaningLog, *, as_of: str | None = None) -> pd.DataFrame:
    df = normalize_text_columns(df, ["county", "service_role", "checkin_method"], "volunteer_shifts", log)
    df["service_date"], _ = parse_mixed_dates(df["service_date"], "volunteer_shifts", "service_date", log)
    df["hours"], unit = parse_quantity_with_unit(df["hours"], "volunteer_shifts", "hours", log, kind="hours")
    df["hours_unit"] = unit
    df["hours_raw"] = df["hours"]  # 保留解析后、截断前的原始值，供异常检测与审计使用
    df = fill_dimension_nulls(df, ["county", "service_role", "checkin_method"], "volunteer_shifts", log)

    # 物理边界：单次服务 0.25 ~ 12 小时（高原地区单日服务上限）
    df, corrected = clip_to_physical_range(df, "hours", 0.25, 12.0, "volunteer_shifts", log)
    df["is_corrected"] = corrected
    df = drop_rows_missing_measures(df, ["hours"], "volunteer_shifts", log)
    # 未来日期（> 数据截止日）不可信 -> 置空后剔除
    cutoff = pd.Timestamp(as_of) if as_of else pd.Timestamp("2100-01-01")
    future = df["service_date"].notna() & (df["service_date"] > cutoff)
    if future.any():
        log.add("volunteer_shifts", "service_date", "future_date", "剔除晚于数据截止日的记录", int(future.sum()))
        df = df.loc[~future]
    # 业务主键：同一志愿者 + 同一项目 + 同一日期 + 同一服务角色 = 同一次服务。
    # 签到小程序重跑、纸质台账二次录入都会产生这类重复。
    df = drop_duplicates(
        df, "volunteer_shifts", log,
        business_keys=["volunteer_id", "project_id", "service_date", "service_role"],
        exact_subset=["shift_id"],
    )
    return df.reset_index(drop=True)


def clean_donors(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    df = normalize_text_columns(df, ["donor_name", "donor_type", "county"], "donors", log)
    df["first_donation_date"], _ = parse_mixed_dates(
        df["first_donation_date"], "donors", "first_donation_date", log
    )
    df = fill_dimension_nulls(df, ["donor_type", "county"], "donors", log)
    return drop_duplicates(df, "donors", log, exact_subset=["donor_id"])


def clean_donations(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    df = normalize_text_columns(df, ["channel", "designation"], "donations", log)
    df["donation_date"], _ = parse_mixed_dates(df["donation_date"], "donations", "donation_date", log)
    df = fill_dimension_nulls(df, ["channel", "designation"], "donations", log)

    # 非定向捐赠在原始数据中「归属项目」为空 —— 这不是脏数据，而是业务语义：
    # 归入 UNKNOWN 维度成员（机构统筹资金），避免在按项目汇总时被静默丢弃。
    raw_proj = df["project_id"].astype(str).str.strip()
    n_unassigned = int((df["project_id"].isna() | raw_proj.isin(["", "nan", "None"])).sum())
    df["project_id"] = df["project_id"].fillna("UNKNOWN").astype(str).str.strip()
    df.loc[df["project_id"].isin(["", "nan", "None"]), "project_id"] = "UNKNOWN"
    log.add("donations", "project_id", "missing_value", "非定向捐赠归入 UNKNOWN（机构统筹）", n_unassigned)

    # 金额策略：负数（退款/录入反向）与超过 100 万元上限（无法核实的极端值）置缺并剔除；
    # 850,000 元这类真实大额定向捐赠必须保留，因此上限设在 100 万元而非 50 万元。
    amount = pd.to_numeric(df["amount_cny"], errors="coerce")
    df["amount_raw"] = amount
    out_of_policy = amount.notna() & ((amount < 0) | (amount > 1_000_000))
    if int(out_of_policy.sum()):
        log.add(
            "donations", "amount_cny", "extreme_outlier",
            "负数或超过 100 万元上限 -> 置缺并剔除（无法核实）", int(out_of_policy.sum()),
            note="保留 85 万元级真实大额捐赠",
        )
    df["amount_cny"] = amount.where(~out_of_policy)
    df["is_corrected"] = out_of_policy
    df = drop_rows_missing_measures(df, ["amount_cny"], "donations", log)
    df = drop_duplicates(df, "donations", log, exact_subset=["donation_id"])
    return df.reset_index(drop=True)


def clean_in_kind(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    df = normalize_text_columns(df, ["item_name", "item_category", "unit", "warehouse"], "in_kind_donations", log)
    df["received_date"], _ = parse_mixed_dates(df["received_date"], "in_kind_donations", "received_date", log)
    df["quantity"], unit = parse_quantity_with_unit(
        df["quantity"], "in_kind_donations", "quantity", log, kind="quantity"
    )
    df["quantity_unit_raw"] = unit
    df = fill_dimension_nulls(df, ["item_category", "warehouse"], "in_kind_donations", log)
    # 入库日期缺失的物资无法归入任何期间，也无法参与任何时间维度分析 -> 剔除并留痕
    bad_date = df["received_date"].isna()
    if int(bad_date.sum()):
        log.add("in_kind_donations", "received_date", "missing_value",
                "剔除入库日期缺失/无法解析的物资记录", int(bad_date.sum()),
                note="日期是事实表的时间锚点，缺失则不可用于任何期间分析")
        df = df.loc[~bad_date].copy()
    df["quantity_raw"] = df["quantity"]
    df, corrected = clip_to_physical_range(df, "quantity", 1.0, 5000.0, "in_kind_donations", log)
    df["is_corrected"] = corrected
    df["est_total_value_cny"] = pd.to_numeric(df["est_total_value_cny"], errors="coerce")
    df, c2 = clip_to_physical_range(
        df, "est_total_value_cny", 0.0, 500_000.0, "in_kind_donations", log
    )
    df["is_corrected"] = df["is_corrected"] | c2
    # 用「单价 × 数量」重算总额，纠正原始表的乘算错误
    recomputed = (df["est_unit_value_cny"].astype(float) * df["quantity"].astype(float)).round(2)
    diff = (recomputed - df["est_total_value_cny"]).abs()
    n_fix = int((diff > 1.0).sum())
    if n_fix:
        log.add("in_kind_donations", "est_total_value_cny", "inconsistent_derived", "按单价×数量重算", n_fix)
    df["est_total_value_cny"] = recomputed
    df = drop_duplicates(df, "in_kind_donations", log, exact_subset=["inkind_id"])
    return df.reset_index(drop=True)


def clean_beneficiaries(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    df = normalize_text_columns(
        df, ["county", "township", "village", "gender", "ethnicity", "household_type"],
        "beneficiaries", log,
    )
    df["enroll_date"], _ = parse_mixed_dates(df["enroll_date"], "beneficiaries", "enroll_date", log)
    df = fill_dimension_nulls(df, ["township", "village", "household_type"], "beneficiaries", log)
    # 年龄越界（如 145 岁）属于不可信值，置缺后剔除；截断到 120 岁会制造"看起来合理"的假数据
    age = pd.to_numeric(df["age"], errors="coerce")
    df["age_raw"] = age
    out_of_range = age.notna() & ~age.between(0, 120)
    if int(out_of_range.sum()):
        log.add("beneficiaries", "age", "extreme_outlier", "越界年龄置缺并剔除", int(out_of_range.sum()))
    df["age"] = age.where(~out_of_range)
    df["is_corrected"] = out_of_range
    df = drop_rows_missing_measures(df, ["age"], "beneficiaries", log)
    df = drop_duplicates(df, "beneficiaries", log, exact_subset=["beneficiary_id"])
    return df.reset_index(drop=True)


def clean_beneficiary_services(df: pd.DataFrame, log: CleaningLog, *, as_of: str | None = None) -> pd.DataFrame:
    df = normalize_text_columns(df, ["service_type", "county"], "beneficiary_services", log)
    df["service_date"], _ = parse_mixed_dates(df["service_date"], "beneficiary_services", "service_date", log)
    df = fill_dimension_nulls(df, ["service_type", "county"], "beneficiary_services", log)
    df, corrected = clip_to_physical_range(df, "duration_hours", 0.25, 12.0, "beneficiary_services", log)
    df["is_corrected"] = corrected
    # 满意度只允许 1~5 分，越界视为录入错误
    sat = pd.to_numeric(df["satisfaction_score"], errors="coerce")
    bad_sat = sat.notna() & ((sat < 1) | (sat > 5))
    if bad_sat.any():
        log.add("beneficiary_services", "satisfaction_score", "out_of_range", "剔除越界评分", int(bad_sat.sum()))
        sat = sat.where(~bad_sat, other=np.nan)
    df["satisfaction_score"] = sat
    if as_of:
        cutoff = pd.Timestamp(as_of)
        future = df["service_date"].notna() & (df["service_date"] > cutoff)
        if future.any():
            log.add("beneficiary_services", "service_date", "future_date", "剔除晚于数据截止日的记录", int(future.sum()))
            df = df.loc[~future]
    # 业务主键去重：一次服务 = 同一对象 + 同一项目 + 同一日期 + 同一服务类型
    df = drop_duplicates(
        df, "beneficiary_services", log,
        business_keys=["beneficiary_id", "project_id", "service_date", "service_type"],
        exact_subset=["service_id"],
    )
    return df.reset_index(drop=True)


def clean_budget(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    df = normalize_text_columns(df, ["budget_category"], "monthly_budget_execution", log)
    df = fill_dimension_nulls(df, ["budget_category"], "monthly_budget_execution", log)
    df["actual_cny"] = pd.to_numeric(df["actual_cny"], errors="coerce")
    df = drop_rows_missing_measures(df, ["actual_cny"], "monthly_budget_execution", log)
    df, corrected = clip_to_physical_range(df, "actual_cny", 0.0, 5_000_000.0, "monthly_budget_execution", log)
    df["is_corrected"] = corrected
    df["month"] = df["month"].astype(str).map(normalize_text)
    df = drop_duplicates(
        df, "monthly_budget_execution", log,
        business_keys=["month", "project_id", "budget_category"],
    )
    return df.reset_index(drop=True)


def clean_survey(df: pd.DataFrame, log: CleaningLog) -> pd.DataFrame:
    """满意度问卷原始表 -> 结构化表（多源数据结构化的核心展示）。"""
    df = df.copy()
    df = normalize_text_columns(
        df, ["project_name_raw", "county_raw", "gender_raw", "open_comment"], "satisfaction_survey_raw", log
    )
    df["submit_time"], _ = parse_mixed_dates(df["submit_time"], "satisfaction_survey_raw", "submit_time", log)

    # 性别：男/M/1 -> 男；女/F/2 -> 女
    gender_map = {"男": "男", "m": "男", "1": "男", "女": "女", "f": "女", "2": "女"}
    raw_g = df["gender_raw"].astype(str).str.lower().str.strip()
    mapped = raw_g.map(gender_map)
    n_unmapped = int(mapped.isna().sum())
    df["gender"] = mapped.fillna(UNKNOWN)
    if n_unmapped:
        log.add("satisfaction_survey_raw", "gender_raw", "nonstandard_code", "归一化性别编码", n_unmapped)

    # 年龄："30" / "30岁" / "三十"
    cn_num = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

    def _age(v: Any) -> float:
        s = str(v)
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        if m:
            return float(m.group(1))
        if s.startswith("三十"):
            return 30.0
        return np.nan

    df["age"] = df["age_raw"].map(_age)
    df["age"] = df["age"].where(df["age"].between(0, 120))
    if df["age"].isna().any():
        log.add("satisfaction_survey_raw", "age_raw", "nonstandard_code", "解析自由文本年龄", int(df["age"].isna().sum()))

    # 行政区划归一化：去后缀 + 模糊匹配
    def _county(v: Any) -> str:
        s = str(v).strip()
        if "香格里拉" in s:
            return "香格里拉市"
        if "维西" in s:
            return "维西傈僳族自治县"
        if "德钦" in s:
            return "德钦县"
        return UNKNOWN

    df["county"] = df["county_raw"].map(_county)
    n_normalized = int((df["county_raw"].astype(str).str.strip() != df["county"]).sum())
    log.add("satisfaction_survey_raw", "county_raw", "nonstandard_code", "行政区划归一化", n_normalized)

    # 李克特量表：文本 -> 1~5
    def _likert(v: Any) -> float:
        s = str(v).strip()
        m = re.search(r"[1-5]", s)
        if m and ("满意" in s or s.isdigit()):
            return float(m.group(0))
        if "非常满意" in s or "很满意" in s:
            return 5.0
        if "满意" in s:
            return 4.0
        if "一般" in s or "还行" in s:
            return 3.0
        if "不太满意" in s:
            return 2.0
        if "不满意" in s:
            return 1.0
        return np.nan

    for col in ("Q1_总体满意度", "Q2_志愿者服务态度"):
        df[f"{col}_score"] = df[col].map(_likert)
        n = int(df[f"{col}_score"].isna().sum())
        if n:
            log.add("satisfaction_survey_raw", col, "nonstandard_code", "李克特文本转 1~5 分", n)
    df["willing_to_rejoin"] = df["Q3_是否愿意再次参与"].map(
        lambda v: "是" if "愿意" in str(v) and "不" not in str(v) else ("否" if "不愿意" in str(v) else "不确定")
    )
    df["frequency_adequate"] = df["Q4_服务频次是否足够"].map(
        lambda v: "足够" if str(v) in {"足够", "基本足够"} else "不足"
    )
    df = df.drop(columns=["gender_raw", "age_raw", "county_raw"], errors="ignore")
    df = drop_duplicates(df, "satisfaction_survey_raw", log, exact_subset=["response_id"])
    return df.reset_index(drop=True)


CLEANERS = {
    "projects": clean_projects,
    "staff": clean_staff,
    "volunteers": clean_volunteers,
    "volunteer_shifts": clean_shifts,
    "donors": clean_donors,
    "donations": clean_donations,
    "in_kind_donations": clean_in_kind,
    "beneficiaries": clean_beneficiaries,
    "beneficiary_services": clean_beneficiary_services,
    "monthly_budget_execution": clean_budget,
    "satisfaction_survey_raw": clean_survey,
}

TABLES: tuple[str, ...] = tuple(CLEANERS)


def read_raw(cfg: Config, name: str) -> pd.DataFrame:
    """读取单张原始表（实际由 :mod:`qingfeng.ingest` 接入层实现）。"""
    from qingfeng.ingest import read_raw_table

    return read_raw_table(cfg, name)


NEEDS_AS_OF: Final = frozenset({"volunteer_shifts", "beneficiary_services"})


def clean_table(df: pd.DataFrame, name: str, log: CleaningLog, *, as_of: str | None = None) -> pd.DataFrame:
    """按表名分发到对应清洗函数。"""
    fn = CLEANERS[name]
    if name in NEEDS_AS_OF:
        return fn(df, log, as_of=as_of)
    return fn(df, log)


def run_cleaning(cfg: Config, raw: dict[str, pd.DataFrame] | None = None) -> tuple[dict[str, pd.DataFrame], CleaningLog]:
    """清洗全部表，并做跨表外键一致性处理。

    Returns
    -------
    (cleaned_tables, log)
    """
    log = CleaningLog()
    raw = raw or {name: read_raw(cfg, name) for name in TABLES}

    cleaned: dict[str, pd.DataFrame] = {}
    for name in TABLES:
        logger.info("清洗 %s（%d 行）", name, len(raw[name]))
        cleaned[name] = clean_table(raw[name], name, log, as_of=cfg.simulation.as_of_date)

    # ---- 跨表外键完整性 ----
    logger.info("校验跨表外键完整性")
    proj_ids = set(cleaned["projects"]["project_id"])
    vol_ids = set(cleaned["volunteers"]["volunteer_id"])
    donor_ids = set(cleaned["donors"]["donor_id"])
    bene_ids = set(cleaned["beneficiaries"]["beneficiary_id"])

    cleaned["volunteer_shifts"], orphan_v = resolve_orphan_keys(
        cleaned["volunteer_shifts"], "volunteer_id", vol_ids, "volunteer_shifts", log
    )
    cleaned["volunteer_shifts"], orphan_p = resolve_orphan_keys(
        cleaned["volunteer_shifts"], "project_id", proj_ids, "volunteer_shifts", log
    )
    cleaned["volunteer_shifts"]["has_orphan_key"] = orphan_v | orphan_p

    cleaned["donations"], orphan_d = resolve_orphan_keys(
        cleaned["donations"], "donor_id", donor_ids, "donations", log
    )
    cleaned["donations"], orphan_dp = resolve_orphan_keys(
        cleaned["donations"], "project_id", proj_ids | {"UNKNOWN", ""}, "donations", log
    )
    cleaned["donations"]["has_orphan_key"] = orphan_d | orphan_dp

    cleaned["beneficiary_services"], orphan_b = resolve_orphan_keys(
        cleaned["beneficiary_services"], "beneficiary_id", bene_ids, "beneficiary_services", log
    )
    cleaned["beneficiary_services"], orphan_bp = resolve_orphan_keys(
        cleaned["beneficiary_services"], "project_id", proj_ids, "beneficiary_services", log
    )
    cleaned["beneficiary_services"]["has_orphan_key"] = orphan_b | orphan_bp

    for name in ("beneficiaries", "monthly_budget_execution", "in_kind_donations"):
        cleaned[name], _ = resolve_orphan_keys(
            cleaned[name], "project_id", proj_ids, name, log
        )
    cleaned["in_kind_donations"], _ = resolve_orphan_keys(
        cleaned["in_kind_donations"], "donor_id", donor_ids, "in_kind_donations", log
    )
    cleaned["donations"] = cleaned["donations"].assign(
        project_id=lambda d: d["project_id"].replace({"": "UNKNOWN"})
    )

    # staff 外键
    staff_ids = set(cleaned["staff"]["staff_id"])
    cleaned["projects"], _ = resolve_orphan_keys(
        cleaned["projects"], "manager_staff_id", staff_ids, "projects", log
    )

    # ---- 跨表修复：用捐赠流水反推缺失的「首次捐赠日期」 ----
    valid_donations = cleaned["donations"].loc[cleaned["donations"]["donor_id"] != "UNKNOWN"]
    first_seen = valid_donations.groupby("donor_id")["donation_date"].min()
    missing = cleaned["donors"]["first_donation_date"].isna()
    recovered = cleaned["donors"].loc[missing, "donor_id"].map(first_seen).dropna()
    if len(recovered):
        cleaned["donors"].loc[recovered.index, "first_donation_date"] = recovered
        log.add(
            "donors", "first_donation_date", "missing_value",
            "跨表反推：由捐赠流水最早日期回填", len(recovered),
            note="单表无法修复，需联接事实表推导",
        )

    # ---- 跨表修复：用服务对象档案回填服务记录缺失的县域 ----
    county_map = cleaned["beneficiaries"].set_index("beneficiary_id")["county"]
    svc = cleaned["beneficiary_services"]
    miss_county = svc["county"].isna() | svc["county"].astype(str).isin(["", "nan"])
    fill = svc.loc[miss_county, "beneficiary_id"].map(county_map)
    n_fill = int(fill.notna().sum())
    if n_fill:
        svc.loc[fill.dropna().index, "county"] = fill.dropna()
        cleaned["beneficiary_services"] = svc
        log.add("beneficiary_services", "county", "missing_value", "跨表反推：由服务对象档案回填县域", n_fill)

    # ---- 跨表修复：志愿者注册日期缺失 -> 用其最早服务日期回填 ----
    join_fill = cleaned["volunteer_shifts"].groupby("volunteer_id")["service_date"].min()
    miss_join = cleaned["volunteers"]["join_date"].isna()
    rec_join = cleaned["volunteers"].loc[miss_join, "volunteer_id"].map(join_fill).dropna()
    if len(rec_join):
        cleaned["volunteers"].loc[rec_join.index, "join_date"] = rec_join
        log.add("volunteers", "join_date", "missing_value",
                "跨表反推：由最早服务日期回填注册日期", len(rec_join))
    n_still = int(cleaned["volunteers"]["join_date"].isna().sum())
    if n_still:
        log.add("volunteers", "join_date", "missing_value",
                "无任何服务记录，注册日期无法反推，保留为空", n_still,
                note="计入注册志愿者总数，但不参与在职时长类指标")

    # ---- 跨表修复：服务对象建档日期缺失 -> 最早服务日期 -> 项目计划开始日 ----
    enroll_fill = cleaned["beneficiary_services"].groupby("beneficiary_id")["service_date"].min()
    miss_enroll = cleaned["beneficiaries"]["enroll_date"].isna()
    rec_enroll = cleaned["beneficiaries"].loc[miss_enroll, "beneficiary_id"].map(enroll_fill).dropna()
    if len(rec_enroll):
        cleaned["beneficiaries"].loc[rec_enroll.index, "enroll_date"] = rec_enroll
        log.add("beneficiaries", "enroll_date", "missing_value",
                "跨表反推：由最早服务日期回填建档日期", len(rec_enroll))
    still = cleaned["beneficiaries"]["enroll_date"].isna()
    if still.any():
        proj_start = cleaned["projects"].set_index("project_id")["planned_start_date"]
        fallback = cleaned["beneficiaries"].loc[still, "project_id"].map(proj_start).dropna()
        if len(fallback):
            cleaned["beneficiaries"].loc[fallback.index, "enroll_date"] = fallback
            log.add("beneficiaries", "enroll_date", "missing_value",
                    "兜底：由项目计划开始日回填建档日期", len(fallback))

    # ---- 跨表修复：签到记录缺失的县域由项目台账回填 ----
    proj_county = cleaned["projects"].set_index("project_id")["county"]
    sh = cleaned["volunteer_shifts"]
    miss_c = sh["county"].isna() | sh["county"].astype(str).isin(["", "nan"])
    fill_c = sh.loc[miss_c, "project_id"].map(proj_county)
    n_fc = int(fill_c.notna().sum())
    if n_fc:
        sh.loc[fill_c.dropna().index, "county"] = fill_c.dropna()
        cleaned["volunteer_shifts"] = sh
        log.add("volunteer_shifts", "county", "missing_value", "跨表反推：由项目台账回填县域", n_fc)

    # ---- 归一化后二次去重 ----
    # 把悬空外键改写成 UNKNOWN 之后，原本互不相同的键可能变成同一个业务键
    # （例如两条孤儿记录 BN-999991 / BN-999992 都变成 UNKNOWN），
    # 因此必须在维度归一化**之后**再做一次业务键去重，否则唯一性约束会失守。
    from qingfeng.tables import business_keys as _bkeys

    for name in ("volunteer_shifts", "donations", "beneficiary_services",
                 "in_kind_donations", "monthly_budget_execution"):
        keys = [k for k in _bkeys(name) if k in cleaned[name].columns]
        if keys:
            before = len(cleaned[name])
            cleaned[name] = cleaned[name].drop_duplicates(subset=keys, keep="first")
            n = before - len(cleaned[name])
            if n:
                log.add(name, "+".join(keys), "duplicate_row",
                        "维度归一化后二次去重", n, note="UNKNOWN 归并产生的重复")

    logger.info("清洗完成：日志共 %d 条动作记录", len(log.actions))
    return cleaned, log


def write_cleaned(cfg: Config, cleaned: dict[str, pd.DataFrame], log: CleaningLog) -> Path:
    """写出清洗结果与清洗日志。"""
    out = cfg.paths.interim
    out.mkdir(parents=True, exist_ok=True)
    for name, df in cleaned.items():
        df.to_csv(out / f"{name}_clean.csv", index=False, encoding="utf-8-sig")
    log_path = cfg.paths.processed / "cleaning_log.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log.to_frame().to_csv(log_path, index=False, encoding="utf-8-sig")
    log.summary().to_csv(cfg.paths.processed / "cleaning_summary.csv", index=False, encoding="utf-8-sig")
    return log_path


def load_cleaned(cfg: Config) -> dict[str, pd.DataFrame]:
    """读取已清洗的中间结果（供 notebook 与校验模块使用）。

    CSV 会把 ``datetime64`` 写成字符串，直接读回会让下游的日期运算报
    ``TypeError``。因此这里按 :mod:`qingfeng.tables` 的日期列定义**还原类型**，
    保证从磁盘读到的对象与内存中清洗出的对象行为一致。
    """
    from qingfeng.tables import date_columns

    out: dict[str, pd.DataFrame] = {}
    for name in TABLES:
        path = cfg.paths.interim_file(f"{name}_clean.csv")
        if not path.is_file():
            raise FileNotFoundError(f"缺少清洗结果 {path}；请先运行 `make clean`。")
        df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
        for col in date_columns(name):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        out[name] = df
    return out
