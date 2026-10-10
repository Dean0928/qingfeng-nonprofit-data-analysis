"""核心指标体系（KPI）计算。

指标体系分五层，回答"做得多少 / 做得活不活 / 做得省不省 / 做得好不好 / 资源结构健不健康"：

============================  ====================================================
层级                           代表指标
============================  ====================================================
规模层 scale                   项目数、注册志愿者数、服务总时长、捐赠总额、受益对象数
活跃层 engagement              活跃志愿者率、人均服务次数、人均服务时长、复访率
效率层 efficiency              单位服务成本、每小时服务成本、捐赠撬动服务时长
质量层 quality                 满意度均值、满意度达标率、项目按期结项率、执行率
结构层 structure               捐赠来源集中度 HHI、机构捐赠占比、双语志愿者占比
数据层 data_quality            字段完整性、异常记录率、修正记录率
============================  ====================================================

每个指标都带**口径公式**（``formula``）与**数据来源表**，保证报告中的数字可被复核。
未指定阈值的指标默认给出中性状态 ``—``；有阈值的指标按
``config.yaml: analysis.kpi.thresholds`` 判定为 达标 / 关注 / 预警。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from qingfeng.config import Config
from qingfeng.logging_utils import get_logger

logger = get_logger("metrics")

STATUS_OK = "达标"
STATUS_WATCH = "关注"
STATUS_ALERT = "预警"
STATUS_NA = "—"


@dataclass(frozen=True)
class KPI:
    """单个指标的元数据 + 取值。"""

    code: str
    name: str
    category: str
    value: float | None
    unit: str
    formula: str
    source_tables: str
    direction: str = "higher_better"  # higher_better | lower_better | neutral
    target: float | None = None
    status: str = STATUS_NA
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "指标代码": self.code,
            "指标名称": self.name,
            "指标层级": self.category,
            # 保留完整精度：HHI 等小量级指标四舍五入到 4 位会损失有效信息，
            # 展示层的格式化交给报告模块处理。
            "指标值": None if self.value is None else float(self.value),
            "单位": self.unit,
            "目标值": self.target,
            "评价方向": {"higher_better": "越高越好", "lower_better": "越低越好", "neutral": "中性"}[
                self.direction
            ],
            "状态": self.status,
            "口径公式": self.formula,
            "数据来源": self.source_tables,
            "说明": self.note,
        }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _safe_div(a: float, b: float) -> float | None:
    if b in (0, None) or pd.isna(b):
        return None
    return float(a) / float(b)


def _status_from_thresholds(value: float | None, th: dict[str, float]) -> tuple[str, float | None]:
    """根据配置阈值判定指标状态。"""
    if value is None or not th:
        return STATUS_NA, None
    if "warn_below" in th:  # 越高越好
        target = th.get("target")
        if target is not None and value >= target:
            return STATUS_OK, target
        if value >= th["warn_below"]:
            return STATUS_WATCH, target
        return STATUS_ALERT, target
    if "warn_above" in th:  # 越低越好
        target = th.get("target")
        if target is not None and value <= target:
            return STATUS_OK, target
        if value <= th["warn_above"]:
            return STATUS_WATCH, target
        return STATUS_ALERT, target
    return STATUS_NA, None


def _valid_facts(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """剔除 UNKNOWN 维度成员（代理键 -1），保证指标口径干净。"""
    if column not in df.columns:
        return df
    return df.loc[df[column] != -1]


# ---------------------------------------------------------------------------
# 规模层
# ---------------------------------------------------------------------------


def _scale_kpis(cfg: Config, star: dict[str, pd.DataFrame]) -> list[KPI]:
    proj = star["dim_project"]
    vol = star["dim_volunteer"]
    svc = _valid_facts(star["fact_volunteer_service"], "volunteer_key")
    don = _valid_facts(star["fact_donation"], "donor_key")
    ik = star["fact_in_kind"]
    bs = star["fact_beneficiary_service"]
    bene = star["dim_beneficiary"]

    n_projects = int((proj["project_key"] != -1).sum())
    n_active_projects = int(proj["status"].isin(["进行中", "筹备中"]).sum())
    n_volunteers = int((vol["volunteer_key"] != -1).sum())
    hours = float(svc["hours"].sum())
    value = float(svc["volunteer_value_cny"].sum())
    donation_total = float(don["amount_cny"].sum())
    inkind_total = float(ik["est_total_value_cny"].sum())
    n_beneficiaries = int((bene["beneficiary_key"] != -1).sum())
    n_services = int(len(bs))

    return [
        KPI("SCALE-01", "在册公益项目数", "规模层", n_projects, "个",
            "COUNT(DISTINCT project_key)，排除 UNKNOWN", "dim_project"),
        KPI("SCALE-02", "在办项目数（进行中+筹备中）", "规模层", n_active_projects, "个",
            "COUNT(*) WHERE status IN ('进行中','筹备中')", "dim_project"),
        KPI("SCALE-03", "注册志愿者总数", "规模层", n_volunteers, "人",
            "COUNT(DISTINCT volunteer_key)，排除 UNKNOWN", "dim_volunteer"),
        KPI("SCALE-04", "志愿服务总时长", "规模层", hours, "小时",
            "SUM(hours)", "fact_volunteer_service"),
        KPI("SCALE-05", "志愿服务经济价值", "规模层", value, "元",
            f"SUM(hours) × {cfg.simulation.volunteer_hour_value_cny:g} 元/小时",
            "fact_volunteer_service",
            note="折算率由《慈善蓝皮书(2025)》口径反推，非官方公布值"),
        KPI("SCALE-06", "现金捐赠总额", "规模层", donation_total, "元",
            "SUM(amount_cny)", "fact_donation"),
        KPI("SCALE-07", "物资捐赠折价总额", "规模层", inkind_total, "元",
            "SUM(est_total_value_cny)", "fact_in_kind"),
        KPI("SCALE-08", "建档服务对象数", "规模层", n_beneficiaries, "人",
            "COUNT(DISTINCT beneficiary_key)，排除 UNKNOWN", "dim_beneficiary"),
        KPI("SCALE-09", "服务提供总人次", "规模层", n_services, "人次",
            "COUNT(*)", "fact_beneficiary_service"),
    ]


# ---------------------------------------------------------------------------
# 活跃层
# ---------------------------------------------------------------------------


def _engagement_kpis(cfg: Config, star: dict[str, pd.DataFrame]) -> list[KPI]:
    vol = star["dim_volunteer"]
    svc = _valid_facts(star["fact_volunteer_service"], "volunteer_key")
    bs = star["fact_beneficiary_service"]

    n_volunteers = int((vol["volunteer_key"] != -1).sum())
    active = svc.groupby("volunteer_key").size()
    n_active = int(active.shape[0])
    rate = _safe_div(n_active, n_volunteers)
    hours = float(svc["hours"].sum())
    per_capita_hours = _safe_div(hours, n_active)
    per_capita_shifts = _safe_div(len(svc), n_active)

    # 复访率：被服务 2 次及以上的对象占比
    svc_per_bene = bs.loc[bs["beneficiary_key"] != -1].groupby("beneficiary_key").size()
    revisit = _safe_div(int((svc_per_bene >= 2).sum()), int(svc_per_bene.shape[0]))

    # 志愿者留存：注册满 12 个月者中，最近 6 个月仍有服务记录的比例
    cutoff = pd.Timestamp(cfg.simulation.as_of_date)
    joined_early = vol.loc[
        (vol["volunteer_key"] != -1) & (pd.to_datetime(vol["join_date"], errors="coerce") <= cutoff - pd.Timedelta(days=365)),
        "volunteer_key",
    ]
    recent_cut = cutoff - pd.Timedelta(days=180)
    recent_dates = pd.to_datetime(
        star["fact_volunteer_service"]["date_key"].astype(str), format="%Y%m%d", errors="coerce"
    )
    recent_vols = set(
        star["fact_volunteer_service"].loc[recent_dates >= recent_cut, "volunteer_key"]
    )
    retention = _safe_div(int(joined_early.isin(recent_vols).sum()), int(len(joined_early)))

    th_rate = cfg.threshold("active_volunteer_rate")
    st_rate, target_rate = _status_from_thresholds(rate, th_rate)

    return [
        KPI("ENG-01", "活跃志愿者数", "活跃层", n_active, "人",
            "COUNT(DISTINCT volunteer_key) WHERE 服务记录数 >= 1", "fact_volunteer_service"),
        KPI("ENG-02", "志愿者活跃率", "活跃层", rate, "比例",
            "活跃志愿者数 / 注册志愿者总数", "fact_volunteer_service, dim_volunteer",
            direction="higher_better", target=target_rate, status=st_rate,
            note="校准基线 28.72%（《慈善蓝皮书(2025)》全国活跃志愿者占比）"),
        KPI("ENG-03", "人均服务时长", "活跃层", per_capita_hours, "小时/人",
            "服务总时长 / 活跃志愿者数", "fact_volunteer_service"),
        KPI("ENG-04", "人均服务次数", "活跃层", per_capita_shifts, "次/人",
            "服务总人次 / 活跃志愿者数", "fact_volunteer_service"),
        KPI("ENG-05", "服务对象复访率", "活跃层", revisit, "比例",
            "服务次数≥2 的服务对象数 / 有服务记录的对象数", "fact_beneficiary_service",
            direction="higher_better"),
        KPI("ENG-06", "志愿者一年留存率", "活跃层", retention, "比例",
            "注册满 12 个月且近 6 个月仍有服务的人数 / 注册满 12 个月人数",
            "dim_volunteer, fact_volunteer_service", direction="higher_better"),
    ]


# ---------------------------------------------------------------------------
# 效率层
# ---------------------------------------------------------------------------


def _efficiency_kpis(cfg: Config, star: dict[str, pd.DataFrame]) -> list[KPI]:
    svc = star["fact_volunteer_service"]
    bs = star["fact_beneficiary_service"]
    don = _valid_facts(star["fact_donation"], "donor_key")
    monthly = star["fact_project_monthly"]

    total_spend = float(monthly["actual_cny"].sum())
    n_services = int(len(bs))
    hours = float(svc["hours"].sum())
    donation_total = float(don["amount_cny"].sum())

    cost_per_service = _safe_div(total_spend, n_services)
    cost_per_hour = _safe_div(total_spend, hours)
    # 每万元捐赠撬动的服务小时（比"小时/元"更易读）
    donation_leverage = None if not donation_total else hours / donation_total * 10_000
    per_project_spend = _safe_div(total_spend, int(monthly["project_id"].nunique()))

    return [
        KPI("EFF-01", "单位服务成本", "效率层", cost_per_service, "元/人次",
            "项目实际支出总额 / 服务提供总人次", "fact_project_monthly, fact_beneficiary_service",
            direction="lower_better"),
        KPI("EFF-02", "每小时服务成本", "效率层", cost_per_hour, "元/小时",
            "项目实际支出总额 / 志愿服务总时长", "fact_project_monthly, fact_volunteer_service",
            direction="lower_better"),
        KPI("EFF-03", "捐赠撬动服务时长", "效率层", donation_leverage, "小时/万元",
            "志愿服务总时长 / 现金捐赠总额 × 10000", "fact_volunteer_service, fact_donation",
            direction="higher_better",
            note="衡量当现金捐赠收缩时，志愿时长能否补位"),
        KPI("EFF-04", "单项目平均支出", "效率层", per_project_spend, "元/个",
            "项目实际支出总额 / 有支出的项目数", "fact_project_monthly", direction="neutral"),
    ]


# ---------------------------------------------------------------------------
# 质量层
# ---------------------------------------------------------------------------


def _quality_kpis(cfg: Config, star: dict[str, pd.DataFrame]) -> list[KPI]:
    proj = star["dim_project"]
    bs = star["fact_beneficiary_service"]
    monthly = star["fact_project_monthly"]

    sat = pd.to_numeric(bs["satisfaction_score"], errors="coerce").dropna()
    sat_mean = float(sat.mean()) if len(sat) else None
    sat_ok = _safe_div(int((sat >= 4).sum()), int(len(sat)))

    planned = float(monthly["planned_cny"].sum())
    actual = float(monthly["actual_cny"].sum())
    exec_rate = _safe_div(actual, planned)

    completed = proj.loc[proj["is_completed"] == 1]
    on_time = _safe_div(
        int((pd.to_numeric(completed["plan_delay_days"], errors="coerce") <= 0).sum()),
        int(len(completed)),
    )
    median_shift = float(pd.to_numeric(star["fact_volunteer_service"]["hours"], errors="coerce").median())

    th_sat = cfg.threshold("satisfaction_mean")
    st_sat, target_sat = _status_from_thresholds(sat_mean, th_sat)
    th_exec = cfg.threshold("project_execution_rate")
    st_exec, target_exec = _status_from_thresholds(exec_rate, th_exec)

    return [
        KPI("QUA-01", "服务满意度均值", "质量层", sat_mean, "分（1-5）",
            "AVG(satisfaction_score)，剔除越界值", "fact_beneficiary_service",
            direction="higher_better", target=target_sat, status=st_sat),
        KPI("QUA-02", "满意度达标率（≥4 分）", "质量层", sat_ok, "比例",
            "满意度 ≥ 4 的记录数 / 有效评分记录数", "fact_beneficiary_service",
            direction="higher_better"),
        KPI("QUA-03", "项目预算执行率", "质量层", exec_rate, "比例",
            "SUM(actual_cny) / SUM(planned_cny)", "fact_project_monthly",
            direction="higher_better", target=target_exec, status=st_exec),
        KPI("QUA-04", "项目按期结项率", "质量层", on_time, "比例",
            "实际结束不晚于计划结束的已结项项目数 / 已结项项目数", "dim_project",
            direction="higher_better"),
        KPI("QUA-05", "单次服务时长中位数", "质量层", median_shift, "小时",
            "MEDIAN(hours)", "fact_volunteer_service", direction="neutral"),
        KPI("QUA-06", "需跟进的服务占比", "质量层",
            _safe_div(int(bs["followup_needed"].sum()), len(bs)), "比例",
            "followup_needed = 1 的记录数 / 服务总人次", "fact_beneficiary_service",
            direction="lower_better"),
    ]


# ---------------------------------------------------------------------------
# 结构层
# ---------------------------------------------------------------------------


def _hhi(shares: np.ndarray) -> float:
    """赫芬达尔-赫希曼指数：份额平方和，越大越集中。"""
    s = np.asarray(shares, dtype=float)
    s = s[np.isfinite(s) & (s > 0)]
    return float(np.sum(s**2)) if s.size else np.nan


def _structure_kpis(cfg: Config, star: dict[str, pd.DataFrame]) -> list[KPI]:
    don = _valid_facts(star["fact_donation"], "donor_key")
    donor = star["dim_donor"]
    vol = star["dim_volunteer"]

    by_donor = don.groupby("donor_key")["amount_cny"].sum()
    total = float(by_donor.sum())
    hhi = _hhi((by_donor / total).to_numpy()) if total else np.nan

    merged = don.merge(donor[["donor_key", "donor_type"]], on="donor_key", how="left")
    inst_share = _safe_div(
        float(merged.loc[merged["donor_type"] != "个人", "amount_cny"].sum()), total
    )
    top10_share = _safe_div(float(by_donor.nlargest(10).sum()), total)
    bilingual = _safe_div(
        int((vol.loc[vol["volunteer_key"] != -1, "has_bilingual_skill"] == 1).sum()),
        int((vol["volunteer_key"] != -1).sum()),
    )
    # 帕累托：前 20% 捐赠人贡献的金额占比
    n_top = max(1, int(np.ceil(len(by_donor) * 0.2)))
    pareto = _safe_div(float(by_donor.nlargest(n_top).sum()), total)

    th_hhi = cfg.threshold("donor_concentration_hhi")
    st_hhi, target_hhi = _status_from_thresholds(hhi, th_hhi)

    return [
        KPI("STR-01", "捐赠来源集中度 HHI", "结构层", hhi, "指数",
            "SUM((单个捐赠人金额 / 捐赠总额)²)", "fact_donation, dim_donor",
            direction="lower_better", target=target_hhi, status=st_hhi,
            note="> 0.25 说明收入过度依赖少数大额捐赠人，抗风险能力弱"),
        KPI("STR-02", "机构捐赠金额占比", "结构层", inst_share, "比例",
            "企业+基金会+政府+社会组织捐赠额 / 捐赠总额", "fact_donation, dim_donor",
            direction="neutral"),
        KPI("STR-03", "前 10 大捐赠人金额占比", "结构层", top10_share, "比例",
            "金额前 10 名捐赠人合计 / 捐赠总额", "fact_donation", direction="lower_better"),
        KPI("STR-04", "帕累托集中度（前 20% 捐赠人）", "结构层", pareto, "比例",
            "金额前 20% 捐赠人合计 / 捐赠总额", "fact_donation", direction="neutral"),
        KPI("STR-05", "双语志愿者占比", "结构层", bilingual, "比例",
            "掌握藏汉双语或傈僳语翻译技能的志愿者数 / 注册志愿者数", "dim_volunteer",
            direction="higher_better",
            note="民族地区服务可达性的关键约束指标"),
    ]


# ---------------------------------------------------------------------------
# 数据质量层
# ---------------------------------------------------------------------------


def _data_quality_kpis(cfg: Config, star: dict[str, pd.DataFrame], cleaned: dict[str, pd.DataFrame] | None) -> list[KPI]:
    frames = {k: v for k, v in star.items()}
    if cleaned:
        frames.update({f"clean_{k}": v for k, v in cleaned.items()})
    total_cells = sum(int(df.shape[0] * df.shape[1]) for df in frames.values())
    total_na = sum(int(df.isna().sum().sum()) for df in frames.values())
    completeness = 1.0 - _safe_div(total_na, total_cells)

    fs = star["fact_volunteer_service"]
    fd = star["fact_donation"]
    fbs = star["fact_beneficiary_service"]
    n_rows = len(fs) + len(fd) + len(fbs)
    n_corrected = int(fs["is_corrected"].sum() + fd["is_corrected"].sum() + fbs["is_corrected"].sum())
    n_orphan = int(fs["has_orphan_key"].sum() + fd["has_orphan_key"].sum() + fbs["has_orphan_key"].sum())
    corrected_rate = _safe_div(n_corrected, n_rows)
    orphan_rate = _safe_div(n_orphan, n_rows)

    th = cfg.threshold("data_completeness")
    st, target = _status_from_thresholds(completeness, th)

    return [
        KPI("DQ-01", "数据完整性", "数据层", completeness, "比例",
            "1 - 缺失单元格数 / 总单元格数", "星型模型 + 清洗结果",
            direction="higher_better", target=target, status=st),
        KPI("DQ-02", "修正记录率", "数据层", corrected_rate, "比例",
            "被截断/置缺修正的事实记录数 / 事实记录总数", "fact_* ，is_corrected 标记",
            direction="lower_better"),
        KPI("DQ-03", "悬空外键记录率", "数据层", orphan_rate, "比例",
            "维度键无法匹配的记录数 / 事实记录总数", "fact_* ，has_orphan_key 标记",
            direction="lower_better"),
    ]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def compute_kpis(
    cfg: Config, star: dict[str, pd.DataFrame], cleaned: dict[str, pd.DataFrame] | None = None
) -> pd.DataFrame:
    """计算全部 KPI，返回长表（一行一个指标）。"""
    kpis: list[KPI] = []
    kpis += _scale_kpis(cfg, star)
    kpis += _engagement_kpis(cfg, star)
    kpis += _efficiency_kpis(cfg, star)
    kpis += _quality_kpis(cfg, star)
    kpis += _structure_kpis(cfg, star)
    kpis += _data_quality_kpis(cfg, star, cleaned)

    df = pd.DataFrame([k.as_dict() for k in kpis])
    logger.info("已计算 %d 个核心指标", len(df))
    n_alert = int((df["状态"] == STATUS_ALERT).sum())
    n_watch = int((df["状态"] == STATUS_WATCH).sum())
    logger.info("指标状态：达标 %d / 关注 %d / 预警 %d",
                int((df["状态"] == STATUS_OK).sum()), n_watch, n_alert)
    return df


def kpi_timeseries(cfg: Config, star: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """月度指标面板：用于趋势分析、异常检测与仪表盘。"""
    svc = star["fact_volunteer_service"].copy()
    don = star["fact_donation"].copy()
    ik = star["fact_in_kind"].copy()
    bs = star["fact_beneficiary_service"].copy()
    monthly = star["fact_project_monthly"].copy()

    svc["month"] = svc["date_key"].astype(str).str.slice(0, 6)
    don["month"] = don["date_key"].astype(str).str.slice(0, 6)
    ik["month"] = ik["date_key"].astype(str).str.slice(0, 6)
    bs["month"] = bs["date_key"].astype(str).str.slice(0, 6)
    monthly["month"] = monthly["year_month"].str.replace("-", "", regex=False)

    def _fmt(s: pd.Series) -> pd.Series:
        return s.str.slice(0, 4) + "-" + s.str.slice(4, 6)

    vol = star["dim_volunteer"]
    reg_cum = (
        vol.loc[vol["volunteer_key"] != -1]
        .assign(month=pd.to_datetime(vol["join_date"], errors="coerce").dt.strftime("%Y%m"))
        .groupby("month")
        .size()
        .rename("new_volunteers")
    )

    panel = pd.DataFrame(
        {
            "service_hours": svc.groupby("month")["hours"].sum(),
            "service_count": svc.groupby("month").size(),
            "active_volunteers": svc.groupby("month")["volunteer_key"].nunique(),
            "volunteer_value_cny": svc.groupby("month")["volunteer_value_cny"].sum(),
            "donation_amount": don.groupby("month")["amount_cny"].sum(),
            "donation_count": don.groupby("month").size(),
            "donor_count": don.groupby("month")["donor_key"].nunique(),
            "inkind_value": ik.groupby("month")["est_total_value_cny"].sum(),
            "service_visits": bs.groupby("month").size(),
            "beneficiaries_served": bs.groupby("month")["beneficiary_key"].nunique(),
            "satisfaction_mean": bs.groupby("month")["satisfaction_score"].mean(),
            "planned_cny": monthly.groupby("month")["planned_cny"].sum(),
            "actual_cny": monthly.groupby("month")["actual_cny"].sum(),
        }
    ).fillna(0.0)

    panel = panel.join(reg_cum, how="outer").fillna(0.0)
    panel.index = _fmt(pd.Series(panel.index, index=panel.index))
    panel.index.name = "year_month"
    panel = panel.sort_index()
    # 截断到配置的数据窗口：dim_volunteer 的注册日期可早于窗口起点（2019 年起的存量志愿者）
    window_start = cfg.simulation.start_date[:7]
    window_end = cfg.simulation.as_of_date[:7]
    panel = panel.loc[(panel.index >= window_start) & (panel.index <= window_end)]
    panel["registered_volunteers_cum"] = panel["new_volunteers"].cumsum()
    panel["active_rate"] = panel["active_volunteers"] / panel["registered_volunteers_cum"].replace(0, np.nan)
    panel["execution_rate"] = panel["actual_cny"] / panel["planned_cny"].replace(0, np.nan)
    panel["avg_shift_hours"] = panel["service_hours"] / panel["service_count"].replace(0, np.nan)
    panel["donation_per_donor"] = panel["donation_amount"] / panel["donor_count"].replace(0, np.nan)
    panel = panel.reset_index()
    panel["month_dt"] = pd.to_datetime(panel["year_month"] + "-01")
    return panel


def dimension_breakdown(
    cfg: Config, star: dict[str, pd.DataFrame], dimension: str, top: int | None = None
) -> pd.DataFrame:
    """按某个维度（county / category / ethnicity / donor_type / household_type ...）汇总核心度量。

    这是"交叉分析"的基础表：一次调用即可得到"谁在贡献服务、谁在接受服务、钱从哪来"。
    """
    svc = _valid_facts(star["fact_volunteer_service"], "volunteer_key")
    proj = star["dim_project"]
    don = _valid_facts(star["fact_donation"], "donor_key")
    donor = star["dim_donor"]
    bs = star["fact_beneficiary_service"]
    bene = star["dim_beneficiary"]

    if dimension not in {"county", "category"}:
        raise ValueError(f"不支持的维度: {dimension}（可选 county / category）")

    # 事实表本身不带 category，通过 project 维度补齐
    proj_dim = proj[["project_key", "category", "county"]].rename(columns={"county": "project_county"})

    if dimension == "county":
        svc_d = svc[["county", "hours", "volunteer_value_cny"]]
        don_d = don.merge(proj_dim, on="project_key", how="left")[["project_county", "amount_cny"]].rename(
            columns={"project_county": "county"}
        )
        bs_d = bs[["county", "service_key", "satisfaction_score"]]
        base = vol_by_county(star).rename_axis("county")
    else:
        svc_d = svc.merge(proj_dim, on="project_key", how="left")[["category", "hours", "volunteer_value_cny"]]
        don_d = don.merge(proj_dim, on="project_key", how="left")[["category", "amount_cny"]]
        bs_d = bs.merge(proj_dim, on="project_key", how="left")[["category", "service_key", "satisfaction_score"]]
        base = (
            proj.loc[proj["project_key"] != -1]
            .groupby("category")
            .agg(
                project_count=("project_key", "size"),
                planned_budget_cny=("planned_budget_cny", "sum"),
                target_beneficiaries=("target_beneficiaries", "sum"),
            )
        )

    svc_agg = svc_d.dropna(subset=[dimension]).groupby(dimension).agg(
        service_hours=("hours", "sum"),
        service_count=("hours", "size"),
        volunteer_value_cny=("volunteer_value_cny", "sum"),
    )
    don_agg = don_d.dropna(subset=[dimension]).groupby(dimension).agg(
        donation_amount=("amount_cny", "sum"), donation_count=("amount_cny", "size")
    )
    svc_agg["hours_per_10k_donation"] = (
        svc_agg["service_hours"] / don_agg["donation_amount"].replace(0, np.nan) * 10_000
    )
    bs_agg = bs_d.dropna(subset=[dimension]).groupby(dimension).agg(
        service_visits=("service_key", "size"), satisfaction_mean=("satisfaction_score", "mean")
    )

    out = base.join([svc_agg, don_agg, bs_agg], how="outer")
    for col in out.columns:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.fillna(0.0).reset_index()
    out = out.rename(columns={out.columns[0]: dimension})
    out = out.sort_values("service_hours", ascending=False).reset_index(drop=True)
    return out.head(top) if top else out


def vol_by_county(star: dict[str, pd.DataFrame]) -> pd.DataFrame:
    vol = star["dim_volunteer"]
    vol = vol.loc[vol["volunteer_key"] != -1]
    return vol.groupby("county").agg(
        registered_volunteers=("volunteer_key", "size"),
        bilingual_volunteers=("has_bilingual_skill", "sum"),
    )


def kpi_catalog(kpis: pd.DataFrame) -> pd.DataFrame:
    """导出指标字典（去掉取值列），用于文档。"""
    return kpis.drop(columns=["指标值", "状态"], errors="ignore")
