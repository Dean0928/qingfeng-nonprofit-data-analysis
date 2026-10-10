"""星型模型构建：清洗后的业务表 -> 维度表 + 事实表。

模型设计
--------
采用 Kimball 星型模型，让"每个业务问题都能用一条 SQL 回答"：

::

    dim_date ──┐
    dim_project ──┤
    dim_volunteer ──┼──> fact_volunteer_service      (志愿服务签到)
    dim_geography ──┤
                    ├──> fact_donation               (现金捐赠)
                    ├──> fact_in_kind                (物资捐赠)
                    ├──> fact_beneficiary_service    (服务提供)
                    └──> fact_project_monthly        (月度预算执行)

约定
----
* **代理键**：每张维度表生成自增整数 ``*_key``，并保留一个键值为 ``-1`` 的
  ``UNKNOWN`` 成员，用于承载源系统中缺失/悬空的维度引用——这样事实表的业务量
  永远不会因为维度缺失而被静默丢弃。
* **退化维度**：事实表同时保留 ``*_id`` 这类源系统业务主键（如 ``donation_id``），
  便于回溯原始单据。
* **一致性维度**：``dim_date`` 与 ``dim_geography`` 为所有事实表共用。
* **质量标签**：事实表携带 ``is_corrected`` / ``has_orphan_key``，让分析可以
  区分"业务真实波动"与"数据修复痕迹"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from qingfeng.config import Config
from qingfeng.logging_utils import get_logger
from qingfeng.tables import ALL_TABLES

logger = get_logger("transform")

UNKNOWN_KEY = -1
UNKNOWN_LABEL = "UNKNOWN"

STAR_TABLES: tuple[str, ...] = (
    "dim_date", "dim_project", "dim_volunteer", "dim_beneficiary", "dim_donor", "dim_geography",
    "fact_volunteer_service", "fact_donation", "fact_in_kind",
    "fact_beneficiary_service", "fact_project_monthly",
)

DIMENSION_TABLES: tuple[str, ...] = (
    "dim_date", "dim_project", "dim_volunteer", "dim_beneficiary", "dim_donor", "dim_geography",
)
FACT_TABLES: tuple[str, ...] = tuple(t for t in STAR_TABLES if t.startswith("fact_"))

SEASON_CN = {12: "冬", 1: "冬", 2: "冬", 3: "春", 4: "春", 5: "春",
             6: "夏", 7: "夏", 8: "夏", 9: "秋", 10: "秋", 11: "秋"}

AGE_BINS = [0, 18, 25, 35, 45, 55, 65, 200]
AGE_LABELS = ["18岁以下", "18-24岁", "25-34岁", "35-44岁", "45-54岁", "55-64岁", "65岁及以上"]


# ---------------------------------------------------------------------------
# 维度表
# ---------------------------------------------------------------------------


def build_dim_date(cfg: Config) -> pd.DataFrame:
    """日期维度：覆盖数据窗口所在年度的完整日历。"""
    start = pd.Timestamp(cfg.simulation.start_date)
    end = pd.Timestamp(cfg.simulation.as_of_date)
    days = pd.date_range(start, end, freq="D")
    df = pd.DataFrame({"full_date": days})
    df["date_key"] = df["full_date"].dt.strftime("%Y%m%d").astype(int)
    df["year"] = df["full_date"].dt.year
    df["quarter"] = df["full_date"].dt.quarter
    df["month"] = df["full_date"].dt.month
    df["month_name"] = df["month"].map(lambda m: f"{m}月")
    df["year_month"] = df["full_date"].dt.strftime("%Y-%m")
    df["year_quarter"] = df["year"].astype(str) + "Q" + df["quarter"].astype(str)
    df["week_of_year"] = df["full_date"].dt.isocalendar().week.astype(int)
    df["day_of_week"] = df["full_date"].dt.dayofweek + 1
    df["day_name"] = df["day_of_week"].map(
        {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
    )
    df["is_weekend"] = df["day_of_week"].isin([6, 7])
    df["day_of_year"] = df["full_date"].dt.dayofyear
    df["is_month_end"] = df["full_date"].dt.is_month_end
    df["season"] = df["month"].map(SEASON_CN)
    df["month_index"] = (df["year"] - start.year) * 12 + df["month"]
    return df[
        ["date_key", "full_date", "year", "quarter", "month", "month_name", "year_month",
         "year_quarter", "week_of_year", "day_of_week", "day_name", "is_weekend",
         "day_of_year", "is_month_end", "season", "month_index"]
    ]


def _attach_surrogate_key(
    df: pd.DataFrame, natural_key: str, key_name: str, *, unknown_label: str = UNKNOWN_LABEL
) -> pd.DataFrame:
    """为维度表生成代理键，并在首行插入 UNKNOWN 成员（键值 -1）。"""
    df = df.reset_index(drop=True).copy()
    df.insert(0, key_name, np.arange(1, len(df) + 1, dtype="int64"))
    unknown = {col: unknown_label for col in df.columns}
    unknown[key_name] = UNKNOWN_KEY
    unknown[natural_key] = unknown_label
    for col, val in unknown.items():
        if pd.api.types.is_numeric_dtype(df[col]):
            unknown[col] = np.nan
        elif pd.api.types.is_datetime64_any_dtype(df[col]):
            unknown[col] = pd.NaT
        elif pd.api.types.is_bool_dtype(df[col]):
            unknown[col] = False
    # 数值维度列在 UNKNOWN 行上留空更符合语义（如"未知"的预算没有意义）
    unknown = {k: (np.nan if pd.api.types.is_numeric_dtype(df[k]) else v) for k, v in unknown.items()}
    unknown[key_name] = UNKNOWN_KEY
    unknown[natural_key] = unknown_label
    unknown_df = pd.DataFrame([unknown])
    for col in df.columns:
        if pd.api.types.is_integer_dtype(df[col]) and col != key_name:
            unknown_df[col] = unknown_df[col].astype("float64")
    return pd.concat([unknown_df[df.columns], df], ignore_index=True)


def build_dim_project(cleaned: dict[str, pd.DataFrame]) -> pd.DataFrame:
    p = cleaned["projects"].copy()
    p["planned_start_date"] = pd.to_datetime(p["planned_start_date"])
    p["planned_end_date"] = pd.to_datetime(p["planned_end_date"])
    p["actual_end_date"] = pd.to_datetime(p["actual_end_date"], errors="coerce")
    p["duration_days"] = (p["planned_end_date"] - p["planned_start_date"]).dt.days
    p["actual_duration_days"] = (p["actual_end_date"] - p["planned_start_date"]).dt.days
    p["plan_delay_days"] = (p["actual_end_date"] - p["planned_end_date"]).dt.days
    p["start_year"] = p["planned_start_date"].dt.year
    p["start_year_month"] = p["planned_start_date"].dt.strftime("%Y-%m")
    p["is_completed"] = p["status"].isin(["已完成", "已结项"])
    p["budget_per_beneficiary"] = (
        p["planned_budget_cny"] / p["target_beneficiaries"].replace(0, np.nan)
    ).round(2)
    p["budget_band"] = pd.cut(
        p["planned_budget_cny"],
        bins=[-1, 100_000, 200_000, 350_000, 600_000, np.inf],
        labels=["10万以下", "10-20万", "20-35万", "35-60万", "60万以上"],
    ).astype(str)
    p["is_cross_year"] = p["planned_start_date"].dt.year != p["planned_end_date"].dt.year
    keep = [
        "project_id", "project_name", "category", "category_code", "county", "township",
        "manager_staff_id", "status", "funding_source", "risk_level", "planned_budget_cny",
        "target_beneficiaries", "planned_start_date", "planned_end_date", "actual_end_date",
        "duration_days", "actual_duration_days", "plan_delay_days", "start_year",
        "start_year_month", "is_completed", "budget_per_beneficiary", "budget_band", "is_cross_year",
    ]
    return _attach_surrogate_key(p[keep], "project_id", "project_key")


def build_dim_volunteer(cleaned: dict[str, pd.DataFrame], as_of: str) -> pd.DataFrame:
    v = cleaned["volunteers"].copy()
    v["join_date"] = pd.to_datetime(v["join_date"])
    cutoff = pd.Timestamp(as_of)
    v["birth_year"] = pd.to_numeric(v["birth_year"], errors="coerce")
    v["age"] = (cutoff.year - v["birth_year"]).astype("Float64")
    v["age_group"] = pd.cut(
        v["age"].astype(float), bins=AGE_BINS, labels=AGE_LABELS, right=False
    ).astype(str)
    v["age_group"] = v["age_group"].replace("nan", UNKNOWN_LABEL)
    v["join_year"] = v["join_date"].dt.year
    v["tenure_days"] = (cutoff - v["join_date"]).dt.days
    v["skill_count"] = v["skills"].fillna("").map(lambda s: 0 if not s else len(str(s).split("|")))
    v["has_bilingual_skill"] = v["skills"].fillna("").str.contains("藏汉双语|傈僳语翻译")
    v["is_active"] = v["status"].isin(["活跃", "新增"])
    v["gender"] = v["gender"].fillna(UNKNOWN_LABEL)
    v["ethnicity"] = v["ethnicity"].fillna(UNKNOWN_LABEL)
    v["education"] = v["education"].fillna(UNKNOWN_LABEL)
    keep = [
        "volunteer_id", "volunteer_name", "gender", "birth_year", "age", "age_group",
        "ethnicity", "county", "township", "occupation", "education", "join_date",
        "join_year", "tenure_days", "skills", "skill_count", "has_bilingual_skill",
        "status", "is_active",
    ]
    return _attach_surrogate_key(v[keep], "volunteer_id", "volunteer_key")


def build_dim_beneficiary(cleaned: dict[str, pd.DataFrame], projects: pd.DataFrame) -> pd.DataFrame:
    b = cleaned["beneficiaries"].copy()
    b["enroll_date"] = pd.to_datetime(b["enroll_date"])
    b["age_group"] = pd.cut(
        b["age"].astype(float), bins=AGE_BINS, labels=AGE_LABELS, right=False
    ).astype(str)
    b["risk_band"] = pd.cut(
        b["risk_score"], bins=[-1, 33, 66, 100], labels=["低风险", "中风险", "高风险"]
    ).astype(str)
    b["is_minor"] = b["age"] < 18
    b["is_elderly"] = b["age"] >= 60
    b["enroll_year"] = b["enroll_date"].dt.year
    b = b.merge(
        projects[["project_id", "category", "project_name"]].rename(
            columns={"category": "project_category", "project_name": "project_name"}
        ),
        on="project_id", how="left",
    )
    # project_id 为 UNKNOWN 的服务对象在维表联接后不会命中任何项目，
    # 若不回填就会在维表里留下 NULL（与其它维度字段的 UNKNOWN 约定不一致）。
    for col in ("project_name", "project_category"):
        if col in b.columns:
            b[col] = b[col].fillna(UNKNOWN_LABEL)
    keep = [
        "beneficiary_id", "project_id", "project_name", "project_category", "county", "township",
        "village", "age", "age_group", "gender", "ethnicity", "household_type", "risk_score",
        "risk_band", "is_minor", "is_elderly", "enroll_date", "enroll_year",
    ]
    return _attach_surrogate_key(b[keep], "beneficiary_id", "beneficiary_key")


def build_dim_donor(cleaned: dict[str, pd.DataFrame], as_of: str) -> pd.DataFrame:
    d = cleaned["donors"].copy()
    d["first_donation_date"] = pd.to_datetime(d["first_donation_date"])
    cutoff = pd.Timestamp(as_of)
    d["relationship_days"] = (cutoff - d["first_donation_date"]).dt.days
    d["first_donation_year"] = d["first_donation_date"].dt.year
    d["is_institutional"] = d["donor_type"] != "个人"
    d["is_local"] = d["county"].isin(["香格里拉市", "维西傈僳族自治县", "德钦县"])
    keep = [
        "donor_id", "donor_name", "donor_type", "county", "is_recurring",
        "is_institutional", "is_local", "first_donation_date", "first_donation_year",
        "relationship_days", "contact_masked",
    ]
    return _attach_surrogate_key(d[keep], "donor_id", "donor_key")


def build_dim_geography(cleaned: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for county, group in cleaned["projects"].groupby("county"):
        for township in sorted(group["township"].dropna().unique()):
            rows.append({"county": county, "township": township})
    geo = pd.DataFrame(rows).drop_duplicates().reset_index(drop=True)
    geo["county_short"] = (
        geo["county"].str.replace("傈僳族自治县", "", regex=False).str.replace("市", "", regex=False)
    )
    geo["is_state_capital"] = geo["county"] == "香格里拉市"
    geo["is_tibetan_area"] = geo["county"].isin(["香格里拉市", "德钦县"])
    # 乡镇名在不同县可能重名，必须用「县|乡镇」复合业务键
    geo["geo_code"] = geo["county"].astype(str) + "|" + geo["township"].astype(str)
    return _attach_surrogate_key(geo, "geo_code", "geo_key")


# ---------------------------------------------------------------------------
# 事实表
# ---------------------------------------------------------------------------


def _key_map(dim: pd.DataFrame, natural: str, key: str) -> dict[Any, int]:
    return dict(zip(dim[natural], dim[key], strict=False))


def build_fact_volunteer_service(
    cleaned: dict[str, pd.DataFrame],
    dim_volunteer: pd.DataFrame,
    dim_project: pd.DataFrame,
    dim_geo: pd.DataFrame,
    hour_value: float,
) -> pd.DataFrame:
    s = cleaned["volunteer_shifts"].copy()
    s["service_date"] = pd.to_datetime(s["service_date"], errors="coerce")
    s = s.loc[s["service_date"].notna()].copy()
    s["date_key"] = s["service_date"].dt.strftime("%Y%m%d").astype(int)
    s["volunteer_key"] = s["volunteer_id"].map(_key_map(dim_volunteer, "volunteer_id", "volunteer_key")).fillna(UNKNOWN_KEY).astype(int)
    s["project_key"] = s["project_id"].map(_key_map(dim_project, "project_id", "project_key")).fillna(UNKNOWN_KEY).astype(int)
    # 签到表本身没有乡镇字段，用项目台账补全后，经「县|乡镇」复合键接入地理维度
    proj_town = cleaned["projects"].set_index("project_id")["township"]
    s["township"] = s["project_id"].map(proj_town)
    s["county"] = s["county"].fillna(UNKNOWN_LABEL)
    s["geo_code"] = s["county"].astype(str) + "|" + s["township"].astype(str)
    s["geo_key"] = s["geo_code"].map(_key_map(dim_geo, "geo_code", "geo_key")).fillna(UNKNOWN_KEY).astype(int)
    s["hours"] = pd.to_numeric(s["hours"], errors="coerce")
    s["volunteer_value_cny"] = (s["hours"] * hour_value).round(2)
    s["is_long_shift"] = s["hours"] > 8
    s = s.rename(columns={"service_role": "service_role", "checkin_method": "checkin_method"})
    s["is_corrected"] = s.get("is_corrected", False)
    s["has_orphan_key"] = s.get("has_orphan_key", False)
    keep = [
        "shift_id", "date_key", "volunteer_key", "project_key", "geo_key",
        "volunteer_id", "project_id", "county", "service_role", "checkin_method",
        "hours", "volunteer_value_cny", "is_long_shift", "is_corrected", "has_orphan_key",
    ]
    out = s[keep].rename(columns={"shift_id": "service_key"})
    return out.dropna(subset=["hours"]).reset_index(drop=True)


def build_fact_donation(
    cleaned: dict[str, pd.DataFrame],
    dim_donor: pd.DataFrame,
    dim_project: pd.DataFrame,
) -> pd.DataFrame:
    d = cleaned["donations"].copy()
    d["donation_date"] = pd.to_datetime(d["donation_date"], errors="coerce")
    d = d.loc[d["donation_date"].notna()].copy()
    d["date_key"] = d["donation_date"].dt.strftime("%Y%m%d").astype(int)
    d["donor_key"] = d["donor_id"].map(_key_map(dim_donor, "donor_id", "donor_key")).fillna(UNKNOWN_KEY).astype(int)
    d["project_key"] = d["project_id"].map(_key_map(dim_project, "project_id", "project_key")).fillna(UNKNOWN_KEY).astype(int)
    d["amount_cny"] = pd.to_numeric(d["amount_cny"], errors="coerce")
    d["amount_band"] = pd.cut(
        d["amount_cny"], bins=[-1, 100, 1000, 10_000, 100_000, np.inf],
        labels=["100元以下", "100-1000元", "1000-1万元", "1-10万元", "10万元以上"],
    ).astype(str)
    d["is_major_gift"] = d["amount_cny"] >= 50_000
    d["year_month"] = d["donation_date"].dt.strftime("%Y-%m")
    keep = [
        "donation_id", "date_key", "donor_key", "project_key", "donor_id", "project_id",
        "amount_cny", "amount_band", "is_major_gift", "channel", "designation",
        "receipt_issued", "year_month", "is_corrected", "has_orphan_key",
    ]
    return d[keep].rename(columns={"donation_id": "donation_key"}).reset_index(drop=True)


def build_fact_in_kind(
    cleaned: dict[str, pd.DataFrame],
    dim_donor: pd.DataFrame,
    dim_project: pd.DataFrame,
) -> pd.DataFrame:
    k = cleaned["in_kind_donations"].copy()
    k["received_date"] = pd.to_datetime(k["received_date"], errors="coerce")
    # 入库日期缺失的物资无法归入任何期间，只能剔出事实表（清洗日志中已记录原因）
    n_nat = int(k["received_date"].isna().sum())
    if n_nat:
        logger.warning("fact_in_kind: 剔除 %d 条入库日期缺失的物资捐赠记录", n_nat)
        k = k.loc[k["received_date"].notna()].copy()
    k["date_key"] = k["received_date"].dt.strftime("%Y%m%d").astype(int)
    k["donor_key"] = k["donor_id"].map(_key_map(dim_donor, "donor_id", "donor_key")).fillna(UNKNOWN_KEY).astype(int)
    k["project_key"] = k["project_id"].map(_key_map(dim_project, "project_id", "project_key")).fillna(UNKNOWN_KEY).astype(int)
    k["year_month"] = k["received_date"].dt.strftime("%Y-%m")
    k["value_band"] = pd.cut(
        k["est_total_value_cny"], bins=[-1, 5_000, 20_000, 100_000, np.inf],
        labels=["5000元以下", "5000-2万元", "2-10万元", "10万元以上"],
    ).astype(str)
    keep = [
        "inkind_id", "date_key", "donor_key", "project_key", "donor_id", "project_id",
        "item_name", "item_category", "quantity", "unit", "est_unit_value_cny",
        "est_total_value_cny", "value_band", "warehouse", "year_month", "is_corrected",
    ]
    return k[keep].rename(columns={"inkind_id": "inkind_key"}).reset_index(drop=True)


def build_fact_beneficiary_service(
    cleaned: dict[str, pd.DataFrame],
    dim_beneficiary: pd.DataFrame,
    dim_project: pd.DataFrame,
    dim_geo: pd.DataFrame,
) -> pd.DataFrame:
    s = cleaned["beneficiary_services"].copy()
    s["service_date"] = pd.to_datetime(s["service_date"], errors="coerce")
    s = s.loc[s["service_date"].notna()].copy()
    s["date_key"] = s["service_date"].dt.strftime("%Y%m%d").astype(int)
    s["beneficiary_key"] = s["beneficiary_id"].map(_key_map(dim_beneficiary, "beneficiary_id", "beneficiary_key")).fillna(UNKNOWN_KEY).astype(int)
    s["project_key"] = s["project_id"].map(_key_map(dim_project, "project_id", "project_key")).fillna(UNKNOWN_KEY).astype(int)
    proj_town = cleaned["projects"].set_index("project_id")["township"]
    s["township"] = s["project_id"].map(proj_town)
    s["geo_code"] = s["county"].astype(str) + "|" + s["township"].astype(str)
    s["geo_key"] = s["geo_code"].map(_key_map(dim_geo, "geo_code", "geo_key")).fillna(UNKNOWN_KEY).astype(int)
    s["satisfaction_score"] = pd.to_numeric(s["satisfaction_score"], errors="coerce")
    s["duration_hours"] = pd.to_numeric(s["duration_hours"], errors="coerce")
    s["is_satisfied"] = s["satisfaction_score"] >= 4
    s["is_dissatisfied"] = s["satisfaction_score"] < 3
    s["followup_needed"] = s["followup_needed"].astype(bool)
    keep = [
        "service_id", "date_key", "beneficiary_key", "project_key", "geo_key",
        "beneficiary_id", "project_id", "service_type", "duration_hours",
        "satisfaction_score", "is_satisfied", "is_dissatisfied", "followup_needed",
        "county", "is_corrected", "has_orphan_key",
    ]
    return s[keep].rename(columns={"service_id": "service_key"}).reset_index(drop=True)


def build_fact_project_monthly(
    cleaned: dict[str, pd.DataFrame], dim_project: pd.DataFrame
) -> pd.DataFrame:
    b = cleaned["monthly_budget_execution"].copy()
    b["project_key"] = b["project_id"].map(_key_map(dim_project, "project_id", "project_key")).fillna(UNKNOWN_KEY).astype(int)
    b["month_start"] = pd.to_datetime(b["month"] + "-01")
    b["date_key"] = b["month_start"].dt.strftime("%Y%m%d").astype(int)
    b["year"] = b["month_start"].dt.year
    b["month_num"] = b["month_start"].dt.month
    b["planned_cny"] = pd.to_numeric(b["planned_cny"], errors="coerce").fillna(0.0)
    b["actual_cny"] = pd.to_numeric(b["actual_cny"], errors="coerce").fillna(0.0)
    b["variance_cny"] = (b["actual_cny"] - b["planned_cny"]).round(2)
    b["execution_rate"] = np.where(b["planned_cny"] > 0, b["actual_cny"] / b["planned_cny"], np.nan)
    b["is_overspend"] = b["execution_rate"] > 1.0
    b["spend_share"] = b["planned_cny"] / b.groupby("month")["planned_cny"].transform("sum")
    keep = [
        "month", "date_key", "project_key", "project_id", "year", "month_num",
        "budget_category", "planned_cny", "actual_cny", "variance_cny",
        "execution_rate", "is_overspend", "spend_share", "is_corrected",
    ]
    return b[keep].rename(columns={"month": "year_month"}).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def build_star_schema(cfg: Config, cleaned: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """由清洗结果构建完整的星型模型。"""
    logger.info("构建星型模型")
    star: dict[str, pd.DataFrame] = {}

    star["dim_date"] = build_dim_date(cfg)
    star["dim_project"] = build_dim_project(cleaned)
    star["dim_volunteer"] = build_dim_volunteer(cleaned, cfg.simulation.as_of_date)
    star["dim_beneficiary"] = build_dim_beneficiary(cleaned, cleaned["projects"])
    star["dim_donor"] = build_dim_donor(cleaned, cfg.simulation.as_of_date)
    star["dim_geography"] = build_dim_geography(cleaned)

    star["fact_volunteer_service"] = build_fact_volunteer_service(
        cleaned, star["dim_volunteer"], star["dim_project"], star["dim_geography"],
        cfg.simulation.volunteer_hour_value_cny,
    )
    star["fact_donation"] = build_fact_donation(cleaned, star["dim_donor"], star["dim_project"])
    star["fact_in_kind"] = build_fact_in_kind(cleaned, star["dim_donor"], star["dim_project"])
    star["fact_beneficiary_service"] = build_fact_beneficiary_service(
        cleaned, star["dim_beneficiary"], star["dim_project"], star["dim_geography"]
    )
    star["fact_project_monthly"] = build_fact_project_monthly(cleaned, star["dim_project"])

    for name, df in star.items():
        logger.info("  %-28s %7d 行 x %2d 列", name, len(df), df.shape[1])
    return star


def write_star_schema(cfg: Config, star: dict[str, pd.DataFrame]) -> None:
    out = cfg.paths.processed
    out.mkdir(parents=True, exist_ok=True)
    for name, df in star.items():
        df.to_csv(out / f"{name}.csv", index=False, encoding="utf-8-sig")


def load_star_schema(cfg: Config) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for name in STAR_TABLES:
        path = cfg.paths.processed_file(f"{name}.csv")
        if not path.is_file():
            raise FileNotFoundError(f"缺少星型模型表 {path}；请先运行 `make model`。")
        out[name] = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    return out


def star_schema_summary(star: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """输出各表行数、列数与粒度说明，写入 README / 报告。"""
    from qingfeng.tables import TABLE_SPECS

    rows = []
    for name, df in star.items():
        rows.append(
            {
                "table": name,
                "table_type": "维度表" if name in DIMENSION_TABLES else "事实表",
                "rows": len(df),
                "columns": df.shape[1],
                "grain": TABLE_SPECS.get(name.replace("dim_", "").replace("fact_", ""), {}).get("grain", ""),
            }
        )
    return pd.DataFrame(rows)
