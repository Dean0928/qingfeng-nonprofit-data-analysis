"""星型模型与指标体系测试。

覆盖两类断言：
1. **结构性约束** —— 代理键唯一、UNKNOWN 成员存在、外键可解析、日期键可联接；
2. **口径正确性** —— 用与生产代码**完全独立**的算法重新计算关键指标并比对，
   防止"用同一个 bug 验证自己"。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qingfeng.transform import DIMENSION_TABLES, FACT_TABLES, STAR_TABLES, UNKNOWN_KEY


# ---------------------------------------------------------------------------
# 星型模型结构
# ---------------------------------------------------------------------------


class TestStarSchema:
    def test_all_tables_present(self, star):
        assert set(star) == set(STAR_TABLES)

    @staticmethod
    def _key_col(table: str) -> str:
        return {"dim_geography": "geo_key", "dim_date": "date_key"}.get(
            table, f"{table.replace('dim_', '')}_key"
        )

    @pytest.mark.parametrize("table", [t for t in DIMENSION_TABLES if t != "dim_date"])
    def test_dimension_has_unknown_member(self, star, table):
        """业务维度必须有 UNKNOWN 成员；日期维不需要（日期缺失由事实表直接剔除）。"""
        df = star[table]
        key_col = self._key_col(table)
        assert key_col in df.columns, f"{table} 缺少代理键 {key_col}"
        assert (df[key_col] == UNKNOWN_KEY).sum() == 1, f"{table} 应恰有一行 UNKNOWN 成员（键值 -1）"

    @pytest.mark.parametrize("table", DIMENSION_TABLES)
    def test_dimension_key_unique(self, star, table):
        key_col = self._key_col(table)
        assert not star[table][key_col].duplicated().any()

    @pytest.mark.parametrize("table", FACT_TABLES)
    def test_fact_has_date_key(self, star, table):
        assert "date_key" in star[table].columns

    def test_fact_date_keys_resolve_to_dim_date(self, star):
        valid = set(star["dim_date"]["date_key"])
        for table in FACT_TABLES:
            keys = set(star[table]["date_key"].dropna().unique())
            missing = keys - valid
            assert not missing, f"{table} 存在无法匹配 dim_date 的日期键，例如 {sorted(missing)[:3]}"

    @pytest.mark.parametrize(
        ("fact", "fk", "dim"),
        [
            ("fact_volunteer_service", "volunteer_key", "dim_volunteer"),
            ("fact_volunteer_service", "project_key", "dim_project"),
            ("fact_volunteer_service", "geo_key", "dim_geography"),
            ("fact_donation", "donor_key", "dim_donor"),
            ("fact_donation", "project_key", "dim_project"),
            ("fact_in_kind", "donor_key", "dim_donor"),
            ("fact_beneficiary_service", "beneficiary_key", "dim_beneficiary"),
            ("fact_beneficiary_service", "project_key", "dim_project"),
            ("fact_project_monthly", "project_key", "dim_project"),
        ],
    )
    def test_foreign_keys_resolve(self, star, fact, fk, dim):
        parent_key = fk.replace("geo_key", "geo_key")
        valid = set(star[dim][parent_key])
        orphans = set(star[fact][fk].dropna().unique()) - valid
        assert not orphans, f"{fact}.{fk} 有 {len(orphans)} 个值无法在 {dim} 中找到"

    def test_measures_have_no_nulls(self, star):
        for table, cols in (
            ("fact_volunteer_service", ["hours", "volunteer_value_cny"]),
            ("fact_donation", ["amount_cny"]),
            ("fact_in_kind", ["quantity", "est_total_value_cny"]),
            ("fact_project_monthly", ["planned_cny", "actual_cny"]),
        ):
            for col in cols:
                assert not star[table][col].isna().any(), f"{table}.{col} 存在空值"

    def test_derived_columns_are_consistent(self, star):
        """派生列的数值关系必须成立（防止特征工程写错公式）。"""
        fs = star["fact_volunteer_service"]
        ratio = (fs["volunteer_value_cny"] / fs["hours"]).round(4)
        assert ratio.nunique() <= 3, "志愿服务价值 ÷ 时长 应恒等于折算率（允许四舍五入差异）"

        m = star["fact_project_monthly"]
        mask = m["planned_cny"] > 0
        recomputed = (m.loc[mask, "actual_cny"] / m.loc[mask, "planned_cny"])
        assert np.allclose(recomputed, m.loc[mask, "execution_rate"], atol=1e-6)

    def test_unknown_member_is_actually_used(self, star):
        """UNKNOWN 成员不应是摆设：缺失维度键的业务量必须真的落在它上面。"""
        used = int((star["fact_donation"]["project_key"] == UNKNOWN_KEY).sum())
        assert used > 0, "非定向捐赠应落在 UNKNOWN 项目成员上"


# ---------------------------------------------------------------------------
# 指标口径
# ---------------------------------------------------------------------------


class TestKPIComputation:
    def test_kpi_catalog_complete(self, kpis):
        assert len(kpis) >= 30
        required = {"指标代码", "指标名称", "指标层级", "指标值", "单位", "口径公式", "数据来源", "状态"}
        assert required <= set(kpis.columns)
        assert kpis["指标代码"].is_unique
        assert set(kpis["状态"]) <= {"达标", "关注", "预警", "—"}

    def test_service_hours_matches_manual_sum(self, kpis, star):
        # 生产口径排除 UNKNOWN 志愿者，测试必须采用同一口径
        svc = star["fact_volunteer_service"]
        expected = svc.loc[svc["volunteer_key"] != UNKNOWN_KEY, "hours"].sum()
        actual = float(kpis.loc[kpis["指标代码"] == "SCALE-04", "指标值"].iloc[0])
        assert actual == pytest.approx(expected, rel=1e-9)

    def test_donation_total_matches_manual_sum(self, kpis, star):
        don = star["fact_donation"]
        expected = don.loc[don["donor_key"] != UNKNOWN_KEY, "amount_cny"].sum()
        actual = float(kpis.loc[kpis["指标代码"] == "SCALE-06", "指标值"].iloc[0])
        assert actual == pytest.approx(expected, rel=1e-9)

    def test_volunteer_value_uses_configured_rate(self, kpis, star, cfg):
        svc = star["fact_volunteer_service"]
        hours = svc.loc[svc["volunteer_key"] != UNKNOWN_KEY, "hours"].sum()
        expected = hours * cfg.simulation.volunteer_hour_value_cny
        actual = float(kpis.loc[kpis["指标代码"] == "SCALE-05", "指标值"].iloc[0])
        assert actual == pytest.approx(expected, rel=1e-6)

    def test_active_volunteer_rate_definition(self, kpis, star):
        """活跃率 = 有签到记录的志愿者数 ÷ 注册志愿者总数，独立重算。"""
        svc = star["fact_volunteer_service"]
        svc = svc.loc[svc["volunteer_key"] != UNKNOWN_KEY]
        n_active = svc["volunteer_key"].nunique()
        vol = star["dim_volunteer"]
        n_total = int((vol["volunteer_key"] != UNKNOWN_KEY).sum())
        expected = n_active / n_total

        reported_active = float(kpis.loc[kpis["指标代码"] == "ENG-01", "指标值"].iloc[0])
        reported_rate = float(kpis.loc[kpis["指标代码"] == "ENG-02", "指标值"].iloc[0])
        assert reported_active == n_active
        assert reported_rate == pytest.approx(expected, rel=1e-6)
        assert 0.0 < reported_rate < 1.0

    def test_hhi_definition(self, kpis, star):
        """HHI 独立重算：份额平方和。"""
        don = star["fact_donation"]
        don = don.loc[don["donor_key"] != UNKNOWN_KEY]
        by_donor = don.groupby("donor_key")["amount_cny"].sum()
        shares = by_donor / by_donor.sum()
        expected = float((shares**2).sum())
        reported = float(kpis.loc[kpis["指标代码"] == "STR-01", "指标值"].iloc[0])
        assert reported == pytest.approx(expected, rel=1e-6)

    def test_coverage_metrics_are_ratios(self, kpis):
        for code in ("ENG-02", "ENG-05", "ENG-06", "QUA-02", "QUA-04", "DQ-01", "DQ-02", "DQ-03"):
            v = kpis.loc[kpis["指标代码"] == code, "指标值"]
            if len(v) and pd.notna(v.iloc[0]):
                assert 0.0 <= float(v.iloc[0]) <= 1.0, f"{code} 不是合法比例：{v.iloc[0]}"

    def test_satisfaction_within_scale(self, kpis):
        v = float(kpis.loc[kpis["指标代码"] == "QUA-01", "指标值"].iloc[0])
        assert 1.0 <= v <= 5.0


class TestMonthlyPanel:
    def test_window_matches_config(self, analysis, cfg):
        panel = analysis["monthly_panel"]
        assert panel["year_month"].min() == cfg.simulation.start_date[:7]
        assert panel["year_month"].max() == cfg.simulation.as_of_date[:7]
        assert panel["year_month"].is_unique

    def test_no_leading_zero_artifact(self, analysis):
        """历史存量志愿者的注册日期不应把面板拉回到数据窗口之前。"""
        panel = analysis["monthly_panel"]
        assert (panel["service_hours"] > 0).all() or panel["service_hours"].gt(0).mean() > 0.9

    def test_cumulative_registration_is_monotonic(self, analysis):
        panel = analysis["monthly_panel"].sort_values("year_month")
        assert panel["registered_volunteers_cum"].is_monotonic_increasing

    def test_derived_ratios(self, analysis):
        panel = analysis["monthly_panel"]
        mask = panel["planned_cny"] > 0
        assert np.allclose(
            panel.loc[mask, "execution_rate"],
            panel.loc[mask, "actual_cny"] / panel.loc[mask, "planned_cny"],
            atol=1e-9,
        )
