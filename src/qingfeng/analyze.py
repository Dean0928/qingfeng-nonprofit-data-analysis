"""分析层：描述性统计、交叉分析、趋势分析、队列与集中度分析。

对应实习岗位描述中的三项核心能力：

* **描述性统计** —— :func:`descriptive_statistics` / :func:`categorical_profile`
* **交叉分析** —— :func:`chi_square_test` + 一组业务交叉表（民族×项目类别、渠道×捐赠人类型…）
* **趋势分析** —— :func:`trend_analysis`（环比/同比/CAGR/移动平均/STL 分解）

以及两个进阶分析，用于支撑"资源配置建议"：

* **队列留存** —— :func:`cohort_retention`
* **集中度与帕累托** —— :func:`concentration_analysis`（含 Gini 系数）

所有函数只依赖星型模型，返回 ``DataFrame`` / ``dict``，便于在 notebook、报告与测试中复用。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from qingfeng.config import Config
from qingfeng.logging_utils import get_logger
from qingfeng.metrics import _valid_facts, kpi_timeseries

logger = get_logger("analyze")

MEASURE_LABELS = {
    "hours": "志愿服务时长（小时）",
    "volunteer_value_cny": "志愿服务经济价值（元）",
    "amount_cny": "单笔捐赠金额（元）",
    "est_total_value_cny": "物资捐赠折价（元）",
    "duration_hours": "单次服务时长（小时）",
    "satisfaction_score": "服务满意度（分）",
    "planned_cny": "月度计划支出（元）",
    "actual_cny": "月度实际支出（元）",
    "risk_score": "服务对象风险分",
}


# ---------------------------------------------------------------------------
# 1. 描述性统计
# ---------------------------------------------------------------------------


def descriptive_statistics(star: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """对八个核心度量的分布做描述性统计（含偏度、峰度、变异系数、离群点占比）。"""
    sources: list[tuple[str, str, pd.Series]] = [
        ("fact_volunteer_service", "hours", star["fact_volunteer_service"]["hours"]),
        ("fact_volunteer_service", "volunteer_value_cny", star["fact_volunteer_service"]["volunteer_value_cny"]),
        ("fact_donation", "amount_cny", star["fact_donation"]["amount_cny"]),
        ("fact_in_kind", "est_total_value_cny", star["fact_in_kind"]["est_total_value_cny"]),
        ("fact_beneficiary_service", "duration_hours", star["fact_beneficiary_service"]["duration_hours"]),
        ("fact_beneficiary_service", "satisfaction_score", star["fact_beneficiary_service"]["satisfaction_score"]),
        ("fact_project_monthly", "planned_cny", star["fact_project_monthly"]["planned_cny"]),
        ("fact_project_monthly", "actual_cny", star["fact_project_monthly"]["actual_cny"]),
    ]
    rows: list[dict[str, Any]] = []
    for table, col, series in sources:
        s = pd.to_numeric(series, errors="coerce").dropna()
        if s.empty:
            continue
        q1, q3 = s.quantile([0.25, 0.75])
        iqr = q3 - q1
        outlier = int(((s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)).sum())
        rows.append(
            {
                "表": table,
                "度量": col,
                "度量名称": MEASURE_LABELS.get(col, col),
                "样本数": int(s.size),
                "均值": round(float(s.mean()), 2),
                "标准差": round(float(s.std(ddof=1)), 2),
                "变异系数": round(float(s.std(ddof=1) / s.mean()), 3) if s.mean() else np.nan,
                "最小值": round(float(s.min()), 2),
                "P25": round(float(q1), 2),
                "中位数": round(float(s.median()), 2),
                "P75": round(float(q3), 2),
                "P95": round(float(s.quantile(0.95)), 2),
                "最大值": round(float(s.max()), 2),
                "偏度": round(float(stats.skew(s)), 3),
                "峰度": round(float(stats.kurtosis(s)), 3),
                "离群点占比": round(outlier / s.size, 4),
            }
        )
    return pd.DataFrame(rows)


def categorical_profile(
    star: dict[str, pd.DataFrame], table: str, column: str, top: int = 15
) -> pd.DataFrame:
    """分类字段的频数分布表（含占比与累计占比）。"""
    df = star[table]
    s = df[column].astype(str)
    vc = s.value_counts()
    out = pd.DataFrame({"取值": vc.index, "频数": vc.to_numpy()})
    out["占比"] = (out["频数"] / out["频数"].sum()).round(4)
    out["累计占比"] = out["占比"].cumsum().round(4)
    return out.head(top).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. 交叉分析
# ---------------------------------------------------------------------------


def chi_square_test(contingency: pd.DataFrame, alpha: float = 0.05) -> dict[str, Any]:
    """卡方独立性检验 + Cramér's V 效应量。

    ``p < alpha`` 说明两个维度**不独立**（存在真实关联），Cramér's V 衡量关联强度：
    0.1 弱 / 0.3 中 / 0.5 强。
    """
    table = contingency.to_numpy(dtype=float)
    if table.size == 0 or table.sum() == 0:
        return {"chi2": np.nan, "p_value": np.nan, "dof": 0, "cramers_v": np.nan,
                "n": 0, "significant": False, "conclusion": "样本不足"}
    chi2, p, dof, _expected = stats.chi2_contingency(table)
    n = table.sum()
    k = min(table.shape) - 1
    cramers_v = float(np.sqrt(chi2 / (n * k))) if n > 0 and k > 0 else np.nan
    if pd.isna(p):
        conclusion = "无法检验（期望频数过低）"
    elif p < alpha:
        strength = "强" if cramers_v >= 0.5 else ("中等" if cramers_v >= 0.3 else "弱")
        conclusion = f"两维度显著相关（p={p:.4g} < {alpha}），关联强度{strength}（V={cramers_v:.3f}）"
    else:
        conclusion = f"未发现显著相关（p={p:.4g} ≥ {alpha}），两维度可视为独立"
    return {
        "chi2": round(float(chi2), 4),
        "p_value": float(p),
        "dof": int(dof),
        "cramers_v": round(cramers_v, 4) if not pd.isna(cramers_v) else np.nan,
        "n": int(n),
        "significant": bool(p < alpha),
        "conclusion": conclusion,
    }


def _crosstab(df: pd.DataFrame, row: str, col: str, normalize: bool = False) -> pd.DataFrame:
    ct = pd.crosstab(df[row], df[col])
    if normalize:
        ct = (ct / ct.to_numpy().sum()).round(4)
    return ct


def build_cross_datasets(cfg: Config, star: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """构造五组业务交叉分析所需的长表。

    每一组都对应一个真实的运营问题，而不是为了统计而统计：

    1. 志愿者民族 × 项目类别 —— 服务供给是否覆盖了各民族聚居区的需求？
    2. 县域 × 服务角色 —— 各县的志愿者承担的角色结构是否有差异？
    3. 捐赠渠道 × 捐赠人类型 —— 机构捐赠与个人捐赠走的渠道是否不同（影响募款投入）？
    4. 家庭类型 × 满意度档 —— 弱势程度更高的家庭是否满意度更低（服务质量公平性）？
    5. 志愿者年龄段 × 服务类型 —— 青年志愿者与中老年志愿者的分工差异。
    """
    svc = _valid_facts(star["fact_volunteer_service"], "volunteer_key")
    vol = star["dim_volunteer"]
    proj = star["dim_project"]
    don = _valid_facts(star["fact_donation"], "donor_key")
    donor = star["dim_donor"]
    bs = _valid_facts(star["fact_beneficiary_service"], "beneficiary_key")
    bene = star["dim_beneficiary"]

    out: dict[str, pd.DataFrame] = {}

    m = svc.merge(vol[["volunteer_key", "ethnicity", "age_group"]], on="volunteer_key", how="left") \
           .merge(proj[["project_key", "category"]], on="project_key", how="left")
    out["志愿者民族×项目类别"] = _crosstab(m, "ethnicity", "category")
    out["县域×服务角色"] = _crosstab(svc, "county", "service_role")
    out["志愿者年龄段×服务类型"] = _crosstab(m, "age_group", "category")

    dm = don.merge(donor[["donor_key", "donor_type"]], on="donor_key", how="left")
    out["捐赠渠道×捐赠人类型"] = _crosstab(dm, "channel", "donor_type")

    bm = bs.merge(bene[["beneficiary_key", "household_type", "age_group"]], on="beneficiary_key", how="left")
    bm["满意度档"] = (
        pd.cut(bm["satisfaction_score"], bins=[0, 2.999, 3.999, 5],
               labels=["低（<3）", "中（3-4）", "高（≥4）"])
        .astype(str)
        .replace("nan", "无评分")
    )
    out["家庭类型×满意度档"] = _crosstab(bm, "household_type", "满意度档")
    out["服务对象年龄段×服务类型"] = _crosstab(bm, "age_group", "service_type")

    return out


def cross_analysis(cfg: Config, star: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """对全部交叉表执行卡方检验，输出结论表。"""
    datasets = build_cross_datasets(cfg, star)
    alpha = float(cfg.analysis.get("cross_tab", {}).get("alpha", 0.05))
    rows = []
    for name, ct in datasets.items():
        res = chi_square_test(ct, alpha=alpha)
        rows.append(
            {
                "交叉分析": name,
                "行维度": ct.index.name or "行",
                "列维度": ct.columns.name or "列",
                "行数": ct.shape[0],
                "列数": ct.shape[1],
                "样本量": res["n"],
                "卡方值": res["chi2"],
                "自由度": res["dof"],
                "p值": res["p_value"],
                "Cramér's V": res["cramers_v"],
                "是否显著": "是" if res["significant"] else "否",
                "结论": res["conclusion"],
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3. 趋势分析
# ---------------------------------------------------------------------------


def _mom_yoy(series: pd.Series) -> pd.DataFrame:
    df = pd.DataFrame({"value": series})
    df["环比增速"] = df["value"].pct_change()
    df["同比增速"] = df["value"].pct_change(12)
    df["3月移动平均"] = df["value"].rolling(3, min_periods=1).mean()
    df["12月移动平均"] = df["value"].rolling(12, min_periods=1).mean()
    return df


def stl_decompose(series: pd.Series, period: int = 6) -> pd.DataFrame:
    """STL 分解：把时间序列拆成 趋势 / 季节 / 残差。

    公益服务具有很强的季节节奏（开学季、暑期支教、冬季严寒），不剥离季节成分就
    直接判断"某月下降＝业务恶化"会得出错误结论。
    """
    s = pd.Series(series).astype(float)
    if s.notna().sum() < period * 2:
        return pd.DataFrame(index=s.index)
    try:
        from statsmodels.tsa.seasonal import STL

        res = STL(s.interpolate(limit_direction="both"), period=period, robust=True).fit()
        return pd.DataFrame(
            {"观测值": s, "趋势": res.trend, "季节": res.seasonal, "残差": res.resid},
            index=s.index,
        )
    except Exception as exc:  # pragma: no cover - 数据不足时降级
        logger.warning("STL 分解失败，退化为移动平均：%s", exc)
        return pd.DataFrame({"观测值": s, "趋势": s.rolling(period, min_periods=1).mean()}, index=s.index)


def trend_analysis(cfg: Config, panel: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """对四个核心时间序列做趋势分析，返回 {指标名: 明细表}。"""
    freq_cfg = cfg.analysis.get("trend", {})
    period = int(freq_cfg.get("stl_period", 6))
    metrics = {
        "志愿服务时长": "service_hours",
        "现金捐赠额": "donation_amount",
        "服务人次": "service_visits",
        "活跃志愿者数": "active_volunteers",
    }
    out: dict[str, pd.DataFrame] = {}
    for label, col in metrics.items():
        if col not in panel.columns:
            continue
        s = panel.set_index("year_month")[col].astype(float)
        detail = _mom_yoy(s)
        decomp = stl_decompose(s, period=period)
        detail = detail.join(decomp, how="left")
        out[label] = detail
    return out


def trend_summary(trends: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """把趋势明细压缩成一张结论表（首末值、CAGR、波动率、季节强度）。"""
    rows = []
    for label, df in trends.items():
        s = df["value"].dropna()
        if len(s) < 3:
            continue
        n = len(s)
        first, last = float(s.iloc[0]), float(s.iloc[-1])
        months = n - 1
        cagr = (last / first) ** (12 / months) - 1 if first > 0 and months > 0 else np.nan
        seasonal_strength = np.nan
        if "季节" in df.columns and df["季节"].notna().any():
            var_resid = float(np.nanvar(df["残差"]))
            var_season = float(np.nanvar(df["季节"]))
            if var_resid > 0:
                seasonal_strength = round(max(0.0, 1 - var_resid / (var_season + var_resid)), 3)
        rows.append(
            {
                "指标": label,
                "起始月": str(s.index[0]),
                "起始值": round(first, 2),
                "最新月": str(s.index[-1]),
                "最新值": round(last, 2),
                "区间变化率": round(last / first - 1, 4) if first else np.nan,
                "年化增长率CAGR": round(cagr, 4) if not pd.isna(cagr) else np.nan,
                "波动率(变异系数)": round(float(s.std(ddof=1) / s.mean()), 4) if s.mean() else np.nan,
                "季节强度": seasonal_strength,
                "最新月环比": round(float(df["环比增速"].dropna().iloc[-1]), 4) if df["环比增速"].notna().any() else np.nan,
                "最新月同比": round(float(df["同比增速"].dropna().iloc[-1]), 4) if df["同比增速"].notna().any() else np.nan,
            }
        )
    return pd.DataFrame(rows)


def monthly_seasonality(panel: pd.DataFrame, metric: str = "service_hours") -> pd.DataFrame:
    """按自然月聚合，识别年度内的服务节奏（用于排班与预算节奏建议）。"""
    df = panel.copy()
    df["month_num"] = pd.to_datetime(df["month_dt"]).dt.month
    g = df.groupby("month_num")[metric].agg(["mean", "std", "count"]).reset_index()
    g = g.rename(columns={"mean": "月均", "std": "标准差", "count": "年数"})
    overall = g["月均"].mean()
    g["相对强度指数"] = (g["月均"] / overall).round(3) if overall else np.nan
    g["月度名称"] = g["month_num"].map(lambda m: f"{m}月")
    return g.sort_values("相对强度指数", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4. 队列留存
# ---------------------------------------------------------------------------


def cohort_retention(star: dict[str, pd.DataFrame], min_year: int | None = None) -> pd.DataFrame:
    """志愿者留存队列：注册年份 × 活跃年份 的留存矩阵。

    行 = 注册年份队列，列 = 活动年份，值 = 该队列在该年仍有服务记录的人数。
    """
    svc = _valid_facts(star["fact_volunteer_service"], "volunteer_key").copy()
    vol = star["dim_volunteer"]
    vol = vol.loc[vol["volunteer_key"] != -1, ["volunteer_key", "join_date"]]
    vol["cohort"] = pd.to_datetime(vol["join_date"], errors="coerce").dt.year
    svc["activity_year"] = pd.to_datetime(
        svc["date_key"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.year
    merged = svc.merge(vol[["volunteer_key", "cohort"]], on="volunteer_key", how="left")
    merged = merged.dropna(subset=["cohort"])
    merged["activity_year"] = merged["activity_year"].astype("Int64")
    matrix = (
        merged.groupby(["cohort", "activity_year"])["volunteer_key"]
        .nunique()
        .unstack(fill_value=0)
        .astype(int)
    )
    matrix.index = matrix.index.astype(int)
    matrix.columns = matrix.columns.astype(int)
    sizes = vol.dropna(subset=["cohort"]).groupby("cohort")["volunteer_key"].nunique()
    matrix.insert(0, "队列规模", sizes.reindex(matrix.index).fillna(0).astype(int))
    if min_year:
        matrix = matrix.loc[matrix.index >= min_year]
    return matrix


def retention_rates(cohort_matrix: pd.DataFrame) -> pd.DataFrame:
    """把留存矩阵换算为留存率（相对队列规模）。"""
    if cohort_matrix.empty:
        return cohort_matrix
    size = cohort_matrix["队列规模"].replace(0, np.nan)
    rates = cohort_matrix.drop(columns=["队列规模"]).div(size, axis=0).round(4)
    rates.insert(0, "队列规模", cohort_matrix["队列规模"])
    return rates


# ---------------------------------------------------------------------------
# 5. 集中度 / 帕累托 / 基尼
# ---------------------------------------------------------------------------


def gini_coefficient(values: np.ndarray) -> float:
    """基尼系数：0 = 完全平均，1 = 完全集中。"""
    x = np.sort(np.asarray(values, dtype=float))
    x = x[np.isfinite(x)]
    if x.size == 0 or x.sum() == 0:
        return float("nan")
    n = x.size
    cum = np.cumsum(x)
    return float((n + 1 - 2 * (cum / cum[-1]).sum()) / n)


def concentration_analysis(star: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """捐赠集中度分析：帕累托曲线 + 基尼系数 + 头部捐赠人画像。"""
    don = _valid_facts(star["fact_donation"], "donor_key")
    donor = star["dim_donor"]

    by_donor = don.groupby("donor_key")["amount_cny"].sum().sort_values(ascending=False)
    total = float(by_donor.sum())
    n = len(by_donor)

    lorenz = pd.DataFrame(
        {
            "捐赠人累计占比": np.arange(1, n + 1) / n,
            "金额累计占比": by_donor.cumsum().to_numpy() / total if total else np.zeros(n),
        }
    )
    # 帕累托点位：前 1%/5%/10%/20%/50% 捐赠人贡献的金额占比
    points = []
    for q in (0.01, 0.05, 0.10, 0.20, 0.50):
        k = max(1, int(np.ceil(n * q)))
        points.append(
            {
                "捐赠人占比": f"前 {q:.0%}",
                "人数": k,
                "金额": round(float(by_donor.head(k).sum()), 2),
                "金额占比": round(float(by_donor.head(k).sum()) / total, 4) if total else np.nan,
            }
        )

    top = by_donor.head(15).reset_index().merge(
        donor[["donor_key", "donor_name", "donor_type", "county", "is_recurring"]],
        on="donor_key", how="left",
    )
    top["金额占比"] = (top["amount_cny"] / total).round(4)
    top["累计占比"] = top["金额占比"].cumsum().round(4)

    return {
        "lorenz": lorenz,
        "pareto_points": pd.DataFrame(points),
        "gini": gini_coefficient(by_donor.to_numpy()),
        "hhi": float(((by_donor / total) ** 2).sum()) if total else np.nan,
        "top_donors": top,
        "n_donors": n,
        "total": total,
    }


# ---------------------------------------------------------------------------
# 6. 项目评分卡与相关性
# ---------------------------------------------------------------------------


def project_scorecard(cfg: Config, star: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """逐项目评分卡：投入（预算）、执行（时长/人次）、产出（服务对象）、质量（满意度）。"""
    proj = star["dim_project"]
    proj = proj.loc[proj["project_key"] != -1].copy()
    svc = star["fact_volunteer_service"]
    bs = star["fact_beneficiary_service"]
    monthly = star["fact_project_monthly"]
    bene = star["dim_beneficiary"]

    agg = proj[
        ["project_key", "project_id", "project_name", "category", "county", "status",
         "planned_budget_cny", "target_beneficiaries", "planned_start_date", "planned_end_date",
         "actual_end_date", "plan_delay_days", "funding_source", "risk_level"]
    ].copy()

    s = svc.groupby("project_key").agg(
        service_hours=("hours", "sum"), service_count=("hours", "size"),
        volunteers=("volunteer_key", "nunique"),
    )
    b = bs.groupby("project_key").agg(
        service_visits=("service_key", "size"),
        beneficiaries=("beneficiary_key", "nunique"),
        satisfaction_mean=("satisfaction_score", "mean"),
        followup_rate=("followup_needed", "mean"),
    )
    m = monthly.groupby("project_key").agg(
        planned_cny=("planned_cny", "sum"), actual_cny=("actual_cny", "sum")
    )
    enrolled = bene.loc[bene["beneficiary_key"] != -1].groupby("project_id").size().rename("enrolled_beneficiaries")

    agg = agg.set_index("project_key")
    agg = agg.join(s).join(b).join(m).reset_index()
    agg = agg.merge(enrolled, on="project_id", how="left")
    agg["execution_rate"] = (agg["actual_cny"] / agg["planned_cny"].replace(0, np.nan)).round(4)
    agg["cost_per_visit"] = (agg["actual_cny"] / agg["service_visits"].replace(0, np.nan)).round(2)
    agg["cost_per_hour"] = (agg["actual_cny"] / agg["service_hours"].replace(0, np.nan)).round(2)
    agg["hours_per_volunteer"] = (agg["service_hours"] / agg["volunteers"].replace(0, np.nan)).round(2)
    agg["target_coverage"] = (
        agg["enrolled_beneficiaries"] / agg["target_beneficiaries"].replace(0, np.nan)
    ).round(4)
    agg["is_on_time"] = pd.to_numeric(agg["plan_delay_days"], errors="coerce") <= 0
    agg["duration_months"] = (
        (pd.to_datetime(agg["planned_end_date"]) - pd.to_datetime(agg["planned_start_date"])).dt.days / 30.4
    ).round(1)
    agg["value_per_yuan"] = (
        agg["service_hours"] * cfg.simulation.volunteer_hour_value_cny / agg["actual_cny"].replace(0, np.nan)
    ).round(3)

    # 综合评分：执行率、目标覆盖率、满意度、成本效率 四个维度标准化后加权
    def _z(col: str, reverse: bool = False) -> pd.Series:
        v = pd.to_numeric(agg[col], errors="coerce")
        if v.notna().sum() < 3:
            return pd.Series(0.5, index=agg.index)
        r = v.rank(pct=True)
        return (1 - r) if reverse else r

    agg["综合评分"] = (
        _z("execution_rate").fillna(0.5) * 0.25
        + _z("target_coverage").fillna(0.5) * 0.25
        + _z("satisfaction_mean").fillna(0.5) * 0.25
        + _z("cost_per_visit", reverse=True).fillna(0.5) * 0.25
    ).round(4)
    agg["评级"] = pd.cut(
        agg["综合评分"], bins=[-1, 0.35, 0.55, 0.75, 2], labels=["D 待改进", "C 合格", "B 良好", "A 优秀"]
    ).astype(str)

    return agg.sort_values("综合评分", ascending=False).reset_index(drop=True)


def correlation_analysis(panel: pd.DataFrame) -> pd.DataFrame:
    """月度核心指标的相关矩阵（Spearman，抗离群点）。"""
    cols = [
        "service_hours", "active_volunteers", "service_count", "service_visits",
        "donation_amount", "donation_count", "donor_count", "satisfaction_mean",
        "actual_cny", "execution_rate",
    ]
    cols = [c for c in cols if c in panel.columns]
    sub = panel[cols].apply(pd.to_numeric, errors="coerce")
    return sub.corr(method="spearman").round(3)


def donation_service_elasticity(panel: pd.DataFrame) -> dict[str, Any]:
    """估计「捐赠额 → 服务时长」的弹性系数。

    这是本项目最有业务价值的量化结论之一：当现金捐赠下滑时，志愿服务时长会同步
    收缩还是能够补位？用双对数 OLS 估计弹性，并对 2026H1 单独估计以检验结构变化。
    """
    df = panel.copy()
    df["service_hours"] = pd.to_numeric(df["service_hours"], errors="coerce")
    df["donation_amount"] = pd.to_numeric(df["donation_amount"], errors="coerce")
    df = df[(df["service_hours"] > 0) & (df["donation_amount"] > 0)].copy()
    if len(df) < 6:
        return {"elasticity": np.nan, "r2": np.nan, "p_value": np.nan, "n": len(df),
                "conclusion": "样本不足，无法估计"}

    def _ols(sub: pd.DataFrame) -> dict[str, Any]:
        x = np.log(sub["donation_amount"].to_numpy())
        y = np.log(sub["service_hours"].to_numpy())
        res = stats.linregress(x, y)
        return {
            "elasticity": round(float(res.slope), 4),
            "intercept": round(float(res.intercept), 4),
            "r2": round(float(res.rvalue**2), 4),
            "p_value": float(res.pvalue),
            "n": int(len(sub)),
        }

    full = _ols(df)
    h1 = df[df["year_month"] >= "2026-01"]
    recent = _ols(h1) if len(h1) >= 4 else {}
    elas = full["elasticity"]
    pval = full["p_value"]
    r2 = full["r2"]
    if pd.isna(elas):
        conclusion = "样本不足，无法估计"
        verdict = "无法判断"
    elif pval >= 0.05:
        # 统计不显著时**不能**按点估计的大小去讲故事——这是相关性分析最常见的误用。
        conclusion = (
            f"未发现统计显著的联动关系（弹性点估计 {elas:.2f}，但 p={pval:.3f} ≥ 0.05，"
            f"R²={r2:.2f}）：月度服务时长的变化主要由志愿者供给与项目排期驱动，"
            "资金波动尚未在月度尺度上传导到服务量"
        )
        verdict = "不显著"
    elif elas > 0.3:
        conclusion = (
            f"捐赠与服务时长同向强联动（弹性 {elas:.2f}，p={pval:.3f}，R²={r2:.2f}）："
            "现金捐赠下滑会直接压缩服务供给，缺乏替代性缓冲"
        )
        verdict = "同向强联动"
    elif elas > 0:
        conclusion = (
            f"捐赠与服务时长同向弱联动（弹性 {elas:.2f}，p={pval:.3f}，R²={r2:.2f}）："
            "服务时长对资金变化不敏感，具备一定缓冲能力"
        )
        verdict = "同向弱联动"
    else:
        conclusion = (
            f"捐赠与服务时长负相关（弹性 {elas:.2f}，p={pval:.3f}）："
            "资金紧张时组织更多地依赖志愿服务时长补位"
        )
        verdict = "负相关（替代效应）"
    return {**full, "elasticity_2026H1": recent.get("elasticity"),
            "significant": bool(pval < 0.05), "verdict": verdict, "conclusion": conclusion}


# ---------------------------------------------------------------------------
# 7. 编排
# ---------------------------------------------------------------------------


def run_analysis(cfg: Config, star: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """执行全部分析，返回一个可直接喂给报告层的字典。"""
    logger.info("开始描述性统计与交叉分析")
    panel = kpi_timeseries(cfg, star)

    result: dict[str, Any] = {
        "monthly_panel": panel,
        "descriptive": descriptive_statistics(star),
        "cross_analysis": cross_analysis(cfg, star),
        "cross_tables": build_cross_datasets(cfg, star),
        "categorical_profiles": {
            "志愿者民族结构": categorical_profile(star, "dim_volunteer", "ethnicity"),
            "志愿者学历结构": categorical_profile(star, "dim_volunteer", "education"),
            "服务对象家庭类型": categorical_profile(star, "dim_beneficiary", "household_type"),
            "捐赠渠道分布": categorical_profile(star, "fact_donation", "channel"),
            "捐赠金额区间": categorical_profile(star, "fact_donation", "amount_band"),
            "项目类别分布": categorical_profile(star, "dim_project", "category"),
        },
        "concentration": concentration_analysis(star),
        "cohort": cohort_retention(star),
        "elasticity": donation_service_elasticity(panel),
        "correlation": correlation_analysis(panel),
        "project_scorecard": project_scorecard(cfg, star),
    }
    logger.info("开始趋势分析")
    trends = trend_analysis(cfg, panel)
    result["trends"] = trends
    result["trend_summary"] = trend_summary(trends)
    result["seasonality"] = monthly_seasonality(panel)
    logger.info("分析完成：共 %d 个分析对象", len([k for k, v in result.items() if isinstance(v, pd.DataFrame)]))
    return result


def write_analysis(cfg: Config, analysis: dict[str, Any]) -> None:
    """把分析结果落盘为 CSV（``data/processed/``）与趋势明细。"""
    out = cfg.paths.processed
    out.mkdir(parents=True, exist_ok=True)
    simple = {
        "descriptive_statistics": analysis["descriptive"],
        "cross_analysis_tests": analysis["cross_analysis"],
        "trend_summary": analysis["trend_summary"],
        "monthly_seasonality": analysis["seasonality"],
        "correlation_matrix": analysis["correlation"],
        "cohort_retention": analysis["cohort"],
        "concentration_pareto": analysis["concentration"]["pareto_points"],
        "top_donors": analysis["concentration"]["top_donors"],
        "project_scorecard": analysis["project_scorecard"],
        "monthly_kpi_panel": analysis["monthly_panel"],
    }
    for name, df in simple.items():
        df.to_csv(out / f"{name}.csv", index=False, encoding="utf-8-sig")
    for name, ct in analysis["cross_tables"].items():
        safe = name.replace("×", "_x_").replace("/", "_")
        ct.to_csv(out / f"crosstab_{safe}.csv", encoding="utf-8-sig")
    for name, df in analysis["trends"].items():
        df.to_csv(out / f"trend_{name}.csv", encoding="utf-8-sig")
    logger.info("分析结果已写入 %s", out)
